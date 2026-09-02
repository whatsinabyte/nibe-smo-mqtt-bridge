"""Integration test: a real bridge startup against real stubs for all
three external interfaces at once (Nibe REST API, MQTT broker, HA
Supervisor REST + WebSocket) simultaneously, plus the real filesystem.

Every other integration suite in this repo (test_api_integration.py,
test_mqtt_broker_integration.py, test_ha_supervisor_integration.py,
test_filesystem_integration.py) proves one interface's failure handling in
isolation. None of them prove the three interact correctly during a real
startup sequence — e.g. that a real Nibe response actually reaches
publish_entity_discovery() and produces a real, valid retained MQTT config
a subscriber can read, while the HA registry watcher and Lovelace
provisioning threads are concurrently doing their own real I/O against the
same process. This drives _build_infrastructure() and
_run_startup_sequence() directly (the two phases generate_nibe_mqtt.main()
itself is documented as delegating everything to — see main()'s own
docstring for why main() itself is not unit tested) against three real
stub servers, then _shutdown() for a real, correct teardown.

Deliberately does NOT test the Lovelace card's own JavaScript — there is
no JS test tooling in this repo, and the card's *consumption* of the
nibe/browser/* MQTT topics documented in docs/card-api.md is a distinct
concern (browser-side) from what this test proves: that the bridge really
publishes valid, well-formed payloads to that contract during a real
startup, which is the bridge-side half of the same guarantee.

Skipped unless NIBE_MQTT_TEST_HOST points at a disposable broker, same as
test_mqtt_broker_integration.py:

    ./dev/mosquitto.sh start
    NIBE_MQTT_TEST_HOST=127.0.0.1 NIBE_MQTT_TEST_PORT=1894 \\
      .venv-check/bin/python -m pytest tests/test_end_to_end_startup.py

Must not be run concurrently, against the same broker, alongside
test_mqtt_broker_integration.py (or another instance of this same suite):
unlike every test in that file, _run_startup_sequence() calls the real,
unscoped scan_mqtt_discovery(), which subscribes to the wildcard
homeassistant/+/+/config across the *entire* broker, not just this test's
own namespaced topic. Confirmed empirically: running both suites at once
against the shared dev broker makes this test pick up the other suite's
own in-flight (and sometimes deliberately malformed) retained configs,
which can perturb decide_startup_action()'s view of "existing entities"
enough to flake the discovery-config assertion below. Run this file on its
own, or serialised after/before the other real-broker suites -- not
combined with them via -n auto/--dist in the same invocation.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any
from unittest.mock import patch

import paho.mqtt.client as mqtt
import pytest

_HOST = os.environ.get("NIBE_MQTT_TEST_HOST")
_PORT = int(os.environ.get("NIBE_MQTT_TEST_PORT", "1894"))

pytestmark = pytest.mark.skipif(
    not _HOST, reason="NIBE_MQTT_TEST_HOST is not set; see this module's docstring"
)

# Namespaced the same way test_mqtt_broker_integration.py namespaces its own
# retained test topics -- avoids colliding with that suite's or a previous
# run's leftover retained state on the shared dev broker.
_WORKER_TAG = f"{os.environ.get('PYTEST_XDIST_WORKER', 'solo')}_{os.getpid()}"
_TEST_POINT_ID = 700000 + (hash(_WORKER_TAG) % 90000)


class _StubNibeDevice:
    """Real HTTP server standing in for the Nibe controller: GET / returns
    device info, GET /points returns one real, classifiable bulk point --
    shaped exactly like a real /points response (see
    test_entity_manager_discovery.py's own _raw_api_response, the same
    field names _fetch_bulk_data() actually reads)."""

    def __init__(self, point_id: int) -> None:
        self.point_id = point_id
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                if self.path == "/points":
                    body = json.dumps(outer._bulk_points()).encode()
                else:
                    body = json.dumps(outer._device_info()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        class _StubServer(ThreadingMixIn, HTTPServer):
            daemon_threads = True

        self._server = _StubServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _device_info(self) -> dict:
        return {
            "product": {
                "manufacturer": "NIBE",
                "name": "S-series",
                "serialNumber": f"E2ETEST{os.getpid()}",
                "firmwareId": "1.0.0",
            }
        }

    def _bulk_points(self) -> dict:
        return {
            str(self.point_id): {
                "title": "E2E Test Outdoor Temperature",
                "description": "",
                "value": {"integerValue": 255, "stringValue": "", "isOk": True},
                "metadata": {
                    "modbusRegisterType": "MODBUS_INPUT_REGISTER",
                    "variableType": "integer",
                    "variableSize": "s16",
                    "minValue": -400,
                    "maxValue": 400,
                    "divisor": 10,
                    "decimal": 1,
                    "unit": "°C",
                    "shortUnit": "°C",
                    "isWritable": False,
                    "intDefaultValue": 0,
                    "stringDefaultValue": "",
                    "change": 1,
                    "modbusRegisterID": self.point_id,
                },
            }
        }

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class _StubSupervisor:
    """Real HTTP server for notify_ha/dismiss_ha, plus a generic WebSocket
    handler for every /core/websocket connection this startup opens
    (the registry watcher's long-lived subscription, refresh_registry()'s
    own connections, and provision_lovelace_ui's several ws_call()s).
    A single generic auth + "answer every {id: N, ...} with a successful,
    empty result" handler is deliberately reused for all of them rather
    than modelling each call site's own message shape -- those individual
    protocols are already covered, call by call, in
    test_ha_supervisor_integration.py; this suite only needs every one of
    them to complete without hanging or crashing the startup sequence."""

    _WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self) -> None:
        self.received_rest: list[dict] = []
        self._stop_event = threading.Event()
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0) or 0)
                body = self.rfile.read(length) if length else b""
                outer.received_rest.append({"path": self.path, "body": body})
                payload = b"{}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:
                if self.path != "/core/websocket":
                    self.send_response(404)
                    self.end_headers()
                    return
                import base64
                import hashlib

                key = self.headers.get("Sec-WebSocket-Key", "")
                accept = base64.b64encode(
                    hashlib.sha1(  # nosec B324 — WS handshake per RFC 6455, not a security use
                        (key + outer._WS_GUID).encode(), usedforsecurity=False
                    ).digest()
                ).decode()
                self.send_response(101, "Switching Protocols")
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()
                with contextlib.suppress(ConnectionError, OSError):
                    outer._generic_ws_conversation(self.connection)

        class _StubServer(ThreadingMixIn, HTTPServer):
            daemon_threads = True

        self._server = _StubServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @staticmethod
    def _ws_frame_send(sock: socket.socket, payload: bytes, opcode: int = 0x1) -> None:
        header = bytes([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header += bytes([length])
        elif length < 65536:
            header += bytes([126]) + length.to_bytes(2, "big")
        else:
            header += bytes([127]) + length.to_bytes(8, "big")
        sock.sendall(header + payload)

    @staticmethod
    def _ws_frame_recv(sock: socket.socket) -> tuple[int, bytes]:
        def _recv_exact(n: int) -> bytes:
            buf = b""
            while len(buf) < n:
                chunk = sock.recv(n - len(buf))
                if not chunk:
                    raise ConnectionError("socket closed mid-frame")
                buf += chunk
            return buf

        first2 = _recv_exact(2)
        opcode = first2[0] & 0x0F
        masked = bool(first2[1] & 0x80)
        length = first2[1] & 0x7F
        if length == 126:
            length = int.from_bytes(_recv_exact(2), "big")
        elif length == 127:
            length = int.from_bytes(_recv_exact(8), "big")
        mask_key = _recv_exact(4) if masked else b""
        payload = _recv_exact(length) if length else b""
        if masked:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    def _generic_ws_conversation(self, sock: socket.socket) -> None:
        self._ws_frame_send(sock, json.dumps({"type": "auth_required"}).encode())
        self._ws_frame_recv(sock)  # the client's {"type": "auth", ...}
        self._ws_frame_send(sock, json.dumps({"type": "auth_ok"}).encode())
        while not self._stop_event.is_set():
            opcode, payload = self._ws_frame_recv(sock)
            if opcode == 0x8:
                return
            if opcode != 0x1:
                continue
            msg = json.loads(payload.decode())
            msg_id = msg.get("id")
            if msg_id is not None:
                self._ws_frame_send(
                    sock,
                    json.dumps(
                        {"id": msg_id, "type": "result", "success": True, "result": []}
                    ).encode(),
                )

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def close(self) -> None:
        self._stop_event.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@contextmanager
def _redirect_supervisor_to(port: int):
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host: str, gai_port: Any, *args: Any, **kwargs: Any) -> Any:
        if host == "supervisor":
            return real_getaddrinfo("127.0.0.1", port, *args, **kwargs)
        return real_getaddrinfo(host, gai_port, *args, **kwargs)

    with patch("socket.getaddrinfo", side_effect=fake_getaddrinfo):
        yield


class TestFullStartupAgainstRealNibeMqttAndSupervisorStubs:
    def test_startup_discovers_the_point_and_publishes_a_real_retained_config(
        self, tmp_path
    ) -> None:
        import generate_nibe_mqtt as gnm

        nibe = _StubNibeDevice(_TEST_POINT_ID)
        supervisor = _StubSupervisor()
        entity_manager = None
        publisher = None
        registry_watcher = None
        mgmt_executor = None
        test_executor = None
        mqtt_client = None
        try:
            cfg = gnm.BridgeConfig(
                api_base_url=nibe.base_url,
                nibe_auth="Basic dGVzdA==",
                mqtt_broker=_HOST or "127.0.0.1",
                mqtt_port=_PORT,
                device_name="E2E Test Device",
                device_id="nibe_e2e_test",
                mode="all",
                poll_interval=30,
            )

            # DynamicPointMap's file fallback defaults to /data/, which does
            # not exist (and should not be touched) outside a real HA add-on
            # install -- point it at a real, writable tmp_path instead so
            # this test exercises the actual code path rather than needing
            # a mocked-away filesystem.
            with (
                patch("nibe_dynamic_map._FILE_FALLBACK", str(tmp_path / "dynamic_point_map.json")),
                patch(
                    "nibe_entity_manager._WANTED_POINTS_FILE", str(tmp_path / "wanted_points.json")
                ),
                _redirect_supervisor_to(supervisor.port),
                patch.dict(os.environ, {"SUPERVISOR_TOKEN": "e2e-test-token"}),
            ):
                (
                    api_client,
                    mqtt_client,
                    response,
                    device_id,
                    shutting_down,
                    set_entity_manager,
                ) = gnm._build_infrastructure(cfg)

                assert response.get("product", {}).get("serialNumber", "").startswith("E2ETEST")

                (
                    entity_manager,
                    publisher,
                    registry_watcher,
                    mgmt_executor,
                    test_executor,
                ) = gnm._run_startup_sequence(
                    cfg,
                    api_client,
                    mqtt_client,
                    response,
                    device_id,
                    "all",
                    set_entity_manager,
                )

                # The real point from the real Nibe stub actually made it
                # through discovery and classification.
                point = entity_manager.all_points_by_id.get(_TEST_POINT_ID)
                assert point is not None, "The stub Nibe device's point was never discovered"
                assert point["entity_type"] == "sensor"

                # And a real, valid retained discovery config for it is
                # readable on the real broker by an independent subscriber
                # -- not just present in this process's own in-memory state.
                from nibe_mqtt_publisher import create_entity_id, t_config

                entity_id = create_entity_id(_TEST_POINT_ID)
                topic = t_config("sensor", entity_id)

                observer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
                received: dict[str, str] = {}
                observer.on_message = lambda _c, _u, m: received.update(
                    {m.topic: m.payload.decode()}
                )
                observer.connect(_HOST or "", _PORT, keepalive=10)
                observer.subscribe(topic)
                observer.loop_start()
                try:
                    deadline = time.monotonic() + 15.0
                    while topic not in received and time.monotonic() < deadline:
                        time.sleep(0.05)
                    assert topic in received, f"No retained discovery config found on {topic}"
                    payload = json.loads(received[topic])
                    assert payload["unique_id"] == f"nibe_{_TEST_POINT_ID}"
                finally:
                    observer.loop_stop()
                    observer.disconnect()

                # The registry watcher's background thread connected to the
                # real Supervisor WS stub without raising.
                deadline = time.monotonic() + 5.0
                while registry_watcher._current_ws is None and time.monotonic() < deadline:
                    time.sleep(0.05)

                # A real, clean shutdown -- exercises the actual teardown
                # path (registry watcher stop, executor drain, offline
                # publishes, MQTT disconnect) against the same real broker,
                # not a mocked stand-in for any of it.
                gnm._shutdown(
                    entity_manager,
                    publisher,
                    mqtt_client,
                    registry_watcher,
                    mgmt_executor,
                    test_executor,
                    shutting_down,
                    lambda: None,
                    remove_frontend=False,
                )
                mqtt_client = None  # _shutdown already disconnected it
        finally:
            if mqtt_client is not None:
                with contextlib.suppress(ConnectionError, OSError):
                    mqtt_client.loop_stop()
                    mqtt_client.disconnect()
            nibe.close()
            supervisor.close()

            # Clean up this test's own retained state so it doesn't leak
            # into a later run on the shared dev broker -- same lesson as
            # test_mqtt_broker_integration.py's own namespacing comment.
            from nibe_mqtt_publisher import create_entity_id, t_config

            cleanup_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            cleanup_client.connect(_HOST or "", _PORT, keepalive=10)
            cleanup_client.loop_start()
            entity_id = create_entity_id(_TEST_POINT_ID)
            cleanup_client.publish(t_config("sensor", entity_id), "", retain=True)
            time.sleep(0.1)
            cleanup_client.loop_stop()
            cleanup_client.disconnect()


class TestSequentialStartupRestoresFromRetainedStateAgainstRealStubs:
    """DOCS.md's own "Restart behaviour" section claims that an app update
    or supervisor restart makes the bridge "reconnect and restore its
    entity list from the broker's retained messages" rather than
    rediscovering from scratch. TestFullStartupAgainstRealNibeMqttAndSupervisorStubs
    above only ever runs a single, fresh-install startup (decide_startup_action()
    returns "apply" there, since no retained entities exist yet) -- it does
    not prove the *restore* path DOCS.md actually documents. This runs two
    real, sequential startups against the same broker: the first
    establishes retained state and shuts down cleanly, the second starts
    fresh (a brand-new EntityManager/publisher/mqtt_client, exactly like a
    real process restart) and must restore that same point from the
    broker's retained messages, going through decide_startup_action()'s
    "restore" branch specifically -- not "apply" (which would produce the
    same *end* state by re-publishing from scratch, silently passing this
    test for the wrong reason if left unchecked)."""

    def test_second_startup_restores_not_reapplies(self, tmp_path) -> None:
        import generate_nibe_mqtt as gnm
        from nibe_mqtt_publisher import create_entity_id, t_config

        point_id = _TEST_POINT_ID + 1
        entity_id = create_entity_id(point_id)
        topic = t_config("sensor", entity_id)

        nibe = _StubNibeDevice(point_id)
        supervisor = _StubSupervisor()
        mqtt_client = None
        try:
            cfg = gnm.BridgeConfig(
                api_base_url=nibe.base_url,
                nibe_auth="Basic dGVzdA==",
                mqtt_broker=_HOST or "127.0.0.1",
                mqtt_port=_PORT,
                device_name="E2E Restart Test Device",
                device_id="nibe_e2e_restart_test",
                mode="all",
                poll_interval=30,
            )

            with (
                patch("nibe_dynamic_map._FILE_FALLBACK", str(tmp_path / "dynamic_point_map.json")),
                patch(
                    "nibe_entity_manager._WANTED_POINTS_FILE", str(tmp_path / "wanted_points.json")
                ),
                _redirect_supervisor_to(supervisor.port),
                patch.dict(os.environ, {"SUPERVISOR_TOKEN": "e2e-test-token"}),
            ):
                # ── First startup: a fresh install, establishes retained state ──
                (
                    api_client,
                    mqtt_client,
                    response,
                    device_id,
                    shutting_down,
                    set_entity_manager,
                ) = gnm._build_infrastructure(cfg)
                (
                    entity_manager,
                    publisher,
                    registry_watcher,
                    mgmt_executor,
                    test_executor,
                ) = gnm._run_startup_sequence(
                    cfg, api_client, mqtt_client, response, device_id, "all", set_entity_manager
                )
                assert point_id in entity_manager.mqtt_enabled_points

                gnm._shutdown(
                    entity_manager,
                    publisher,
                    mqtt_client,
                    registry_watcher,
                    mgmt_executor,
                    test_executor,
                    shutting_down,
                    lambda: None,
                    remove_frontend=False,
                )
                mqtt_client = None  # _shutdown already disconnected it

                # ── Second startup: a real process restart. Wrap
                # decide_startup_action so we can see which branch it
                # actually took without changing its real behaviour --
                # Mock's `wraps=` doesn't expose the wrapped call's return
                # value directly, so a plain side_effect records it instead.
                captured_actions: list[str] = []
                real_decide_startup_action = gnm.decide_startup_action

                def _spy_decide_startup_action(*args: Any, **kwargs: Any) -> str:
                    result = real_decide_startup_action(*args, **kwargs)
                    captured_actions.append(result)
                    return result

                with patch(
                    "generate_nibe_mqtt.decide_startup_action",
                    side_effect=_spy_decide_startup_action,
                ):
                    (
                        api_client2,
                        mqtt_client,
                        response2,
                        device_id2,
                        shutting_down2,
                        set_entity_manager2,
                    ) = gnm._build_infrastructure(cfg)
                    (
                        entity_manager2,
                        publisher2,
                        registry_watcher2,
                        mgmt_executor2,
                        test_executor2,
                    ) = gnm._run_startup_sequence(
                        cfg,
                        api_client2,
                        mqtt_client,
                        response2,
                        device_id2,
                        "all",
                        set_entity_manager2,
                    )

                    assert len(captured_actions) == 1
                    action_taken = captured_actions[0]
                    assert action_taken == "restore", (
                        f"Expected the second startup to take the 'restore' path "
                        f"described in DOCS.md, got {action_taken!r} -- either "
                        f"nothing was actually retained from the first startup, "
                        f"or this is silently taking the 'apply' path instead"
                    )
                    assert point_id in entity_manager2.mqtt_enabled_points, (
                        "The point from the first startup was not restored on the second"
                    )

                gnm._shutdown(
                    entity_manager2,
                    publisher2,
                    mqtt_client,
                    registry_watcher2,
                    mgmt_executor2,
                    test_executor2,
                    shutting_down2,
                    lambda: None,
                    remove_frontend=True,  # clean up retained state via the real path
                )
                mqtt_client = None
        finally:
            if mqtt_client is not None:
                with contextlib.suppress(ConnectionError, OSError):
                    mqtt_client.loop_stop()
                    mqtt_client.disconnect()
            nibe.close()
            supervisor.close()

            cleanup_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            cleanup_client.connect(_HOST or "", _PORT, keepalive=10)
            cleanup_client.loop_start()
            cleanup_client.publish(topic, "", retain=True)
            time.sleep(0.1)
            cleanup_client.loop_stop()
            cleanup_client.disconnect()
