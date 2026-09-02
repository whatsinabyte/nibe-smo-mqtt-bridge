"""Integration tests: notify_ha/dismiss_ha and HAEntityRegistryWatcher
against a real HTTP + WebSocket server standing in for the HA Supervisor.

Every call site in nibe_ha_integration.py hardcodes "http://supervisor/..."
and "ws://supervisor/core/websocket" -- unlike NibeApiClient, there is no
base_url parameter to redirect. "supervisor" only resolves inside the real
Supervisor's own Docker network, and binding a local stub to the implicit
port 80 those URLs use needs root. Both problems are solved the same way,
without touching production code: socket.getaddrinfo is monkeypatched for
the duration of each test so that resolving the host "supervisor" returns
this stub server's real (ephemeral, unprivileged) address instead -- every
socket urllib and websocket-client open past that point is still a real
TCP connection doing real HTTP/WS framing, just physically pointed here.

This suite is not just the vanilla/happy-path handshake. The real HA
WebSocket protocol (auth_required -> auth -> auth_ok, subscribe_events,
ping/pong keepalive, reconnect-with-backoff, a give-up-after-N-failures
ceiling) has real failure modes a mocked `websocket.create_connection`
can never expose: a real dropped connection mid-handshake, a real missing
pong, a real auth rejection response. See each test class's docstring for
which specific misbehaviour it proves this code actually survives.

No external service, no Docker, no opt-in env var -- part of the normal,
always-run suite.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
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

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# ============================================================================
# Minimal raw WebSocket framing -- just enough of RFC 6455 for
# websocket-client to hold a real conversation with us: text frames,
# close frames, ping/pong, and unmasking client->server frames (a real
# client always masks; a real server never does).
# ============================================================================


def _ws_accept_key(client_key: str) -> str:
    digest = hashlib.sha1((client_key + _WS_GUID).encode(), usedforsecurity=False).digest()
    return base64.b64encode(digest).decode()


def _ws_send(sock: socket.socket, payload: bytes, opcode: int = 0x1) -> None:
    header = bytes([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header += bytes([length])
    elif length < 65536:
        header += bytes([126]) + length.to_bytes(2, "big")
    else:
        header += bytes([127]) + length.to_bytes(8, "big")
    sock.sendall(header + payload)


def _ws_send_text(sock: socket.socket, text: str) -> None:
    _ws_send(sock, text.encode(), opcode=0x1)


def _ws_send_close(sock: socket.socket, code: int = 1000, reason: str = "") -> None:
    """A real WS close frame (opcode 0x8) -- a graceful, intentional
    server-side close (e.g. Supervisor restarting cleanly), distinct from
    the raw severed-socket tests elsewhere in this file, which instead
    simulate a crash with no closing handshake at all."""
    _ws_send(sock, code.to_bytes(2, "big") + reason.encode(), opcode=0x8)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Socket closed while reading a WS frame")
        buf += chunk
    return buf


def _ws_recv(sock: socket.socket) -> tuple[int, bytes]:
    """Returns (opcode, payload). Raises ConnectionError on a closed socket."""
    first2 = _recv_exact(sock, 2)
    opcode = first2[0] & 0x0F
    masked = bool(first2[1] & 0x80)
    length = first2[1] & 0x7F
    if length == 126:
        length = int.from_bytes(_recv_exact(sock, 2), "big")
    elif length == 127:
        length = int.from_bytes(_recv_exact(sock, 8), "big")
    mask_key = _recv_exact(sock, 4) if masked else b""
    payload = _recv_exact(sock, length) if length else b""
    if masked:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def _ws_recv_text(sock: socket.socket) -> str:
    opcode, payload = _ws_recv(sock)
    if opcode == 0x8:
        raise ConnectionError("Client sent a WS close frame")
    return payload.decode()


# ============================================================================
# Stub server: real HTTP for notify_ha/dismiss_ha, real WS upgrade + framing
# for the entity registry watcher. Each test supplies its own ws_handler
# callback -- there is no generic scripting DSL here, because the five
# misbehaviour scenarios below each need a genuinely different, hand-written
# conversation (drop mid-handshake, withhold a pong, reject auth, ...) that
# a declarative script would just make harder to read, not easier.
# ============================================================================


class _HAStubServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class _StubSupervisor:
    def __init__(self, ws_handler: Any = None, bad_status_count: int = 0) -> None:
        self.received_rest: list[dict] = []
        self.ws_handler = ws_handler
        # Number of /core/websocket upgrade *attempts* to answer with a real
        # non-101 HTTP status (502, matching a real HA Core mid-restart
        # response seen in production) before actually upgrading -- see
        # TestWebSocketUpgradeRejectedAgainstARealServer below.
        self.bad_status_count = bad_status_count
        self._upgrade_attempts = 0
        self._upgrade_attempts_lock = threading.Lock()
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0) or 0)
                body = self.rfile.read(length) if length else b""
                outer.received_rest.append(
                    {"path": self.path, "headers": dict(self.headers), "body": body}
                )
                payload = b"{}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:
                if self.path != "/core/websocket" or outer.ws_handler is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                with outer._upgrade_attempts_lock:
                    outer._upgrade_attempts += 1
                    attempt_no = outer._upgrade_attempts
                if attempt_no <= outer.bad_status_count:
                    # A real HTTP response to the upgrade GET, never
                    # switching protocols -- exactly what HA Core returns
                    # (via its own reverse-proxying Supervisor) while it is
                    # mid-restart, before the WS handshake ever begins.
                    body = b"502: Bad Gateway"
                    self.send_response(502, "Bad Gateway")
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                key = self.headers.get("Sec-WebSocket-Key", "")
                accept = _ws_accept_key(key)
                self.send_response(101, "Switching Protocols")
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()
                # Hand the raw, now-upgraded socket to the test's own
                # handler. It owns the connection from here on -- this
                # method must not return until the conversation is done,
                # since BaseHTTPRequestHandler closes the connection on
                # do_GET() returning otherwise.
                with contextlib.suppress(ConnectionError, OSError):
                    outer.ws_handler(self.connection)

        self._server = _HAStubServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@contextmanager
def _redirect_supervisor_to(port: int):
    """Monkeypatch socket.getaddrinfo so any connection to host "supervisor"
    (whatever port the caller asked for -- both the hardcoded :80 REST URLs
    and the hardcoded :80 WS URL use none explicitly, which defaults to 80)
    transparently lands on our local stub instead. See this module's
    docstring for why this is necessary rather than optional."""
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host: str, gai_port: Any, *args: Any, **kwargs: Any) -> Any:
        if host == "supervisor":
            return real_getaddrinfo("127.0.0.1", port, *args, **kwargs)
        return real_getaddrinfo(host, gai_port, *args, **kwargs)

    with patch("socket.getaddrinfo", side_effect=fake_getaddrinfo):
        yield


@contextmanager
def _supervisor_token(value: str = "test-token"):
    with patch.dict(os.environ, {"SUPERVISOR_TOKEN": value}):
        yield


def _auth_ok_handler(post_auth: Any) -> Any:
    """Builds a ws_handler that performs a normal auth_required/auth/auth_ok
    handshake, then hands off to *post_auth* for whatever the test wants to
    happen next."""

    def handler(sock: socket.socket) -> None:
        _ws_send_text(sock, json.dumps({"type": "auth_required"}))
        _ws_recv_text(sock)  # the client's {"type": "auth", ...}
        _ws_send_text(sock, json.dumps({"type": "auth_ok"}))
        post_auth(sock)

    return handler


class TestNotifyAndDismissAgainstARealServer:
    def test_notify_ha_posts_to_the_real_notification_endpoint(self) -> None:
        from nibe_ha_integration import notify_ha

        stub = _StubSupervisor()
        try:
            with _redirect_supervisor_to(stub.port), _supervisor_token():
                notify_ha(None, "Title", "Message body", "notif_id")
            assert len(stub.received_rest) == 1
            assert (
                stub.received_rest[0]["path"] == "/core/api/services/persistent_notification/create"
            )
            body = json.loads(stub.received_rest[0]["body"])
            assert body["notification_id"] == "notif_id"
            assert body["title"] == "Title"
        finally:
            stub.close()

    def test_dismiss_ha_posts_to_the_real_dismiss_endpoint(self) -> None:
        from nibe_ha_integration import dismiss_ha

        stub = _StubSupervisor()
        try:
            with _redirect_supervisor_to(stub.port), _supervisor_token():
                dismiss_ha(None, "notif_id")
            assert len(stub.received_rest) == 1
            assert (
                stub.received_rest[0]["path"]
                == "/core/api/services/persistent_notification/dismiss"
            )
        finally:
            stub.close()


class TestEntityRegistryWatcherHappyPathAgainstARealServer:
    def test_refresh_registry_authenticates_and_populates_the_map(self) -> None:
        from nibe_ha_integration import HAEntityRegistryWatcher

        def post_auth(sock: socket.socket) -> None:
            req = json.loads(_ws_recv_text(sock))
            assert req["type"] == "config/entity_registry/list"
            _ws_send_text(
                sock,
                json.dumps(
                    {
                        "id": req["id"],
                        "type": "result",
                        "success": True,
                        "result": [
                            {"unique_id": "nibe_100", "entity_id": "sensor.nibe_100"},
                            {"unique_id": "nibe_200", "entity_id": "sensor.nibe_200"},
                            {"unique_id": "other_thing", "entity_id": "sensor.other"},
                        ],
                    }
                ),
            )

        stub = _StubSupervisor(ws_handler=_auth_ok_handler(post_auth))
        try:
            watcher = HAEntityRegistryWatcher.__new__(HAEntityRegistryWatcher)
            watcher._registry_map_lock = threading.Lock()
            watcher._unique_id_map = {}
            with _redirect_supervisor_to(stub.port), _supervisor_token():
                watcher.refresh_registry()
            assert watcher.entity_id_for(100) == "sensor.nibe_100"
            assert watcher.entity_id_for(200) == "sensor.nibe_200"
        finally:
            stub.close()


def _run_loop_handshake(sock: socket.socket, *, subscribe_success: bool = True) -> int | None:
    """Performs the same sequence _connect_and_subscribe() drives in
    production: auth, subscribe_events, then (only if the subscription
    succeeded) the initial full registry fetch. Returns the subscription
    request's id so a caller can send unsolicited events tagged with it,
    or None if the handshake stopped at a failed subscription."""
    _ws_send_text(sock, json.dumps({"type": "auth_required"}))
    _ws_recv_text(sock)
    _ws_send_text(sock, json.dumps({"type": "auth_ok"}))
    sub_req = json.loads(_ws_recv_text(sock))
    _ws_send_text(
        sock, json.dumps({"id": sub_req["id"], "type": "result", "success": subscribe_success})
    )
    if not subscribe_success:
        return None
    reg_req = json.loads(_ws_recv_text(sock))
    _ws_send_text(
        sock, json.dumps({"id": reg_req["id"], "type": "result", "success": True, "result": []})
    )
    return sub_req["id"]


def _make_run_loop_watcher(
    *, initial_backoff: float = 0.2, max_backoff: float = 0.5, max_consec_failures: int = 10
):
    """A HAEntityRegistryWatcher ready to run its real _run() loop in a
    background thread, with the production timing constants shortened so
    tests observe reconnect/give-up behaviour in seconds, not the real
    30s/15s/300s/10-failure budget."""
    from nibe_ha_integration import HAEntityRegistryWatcher

    watcher = HAEntityRegistryWatcher.__new__(HAEntityRegistryWatcher)
    watcher._registry_map_lock = threading.Lock()
    watcher._unique_id_map = {}
    watcher._ws_lock = threading.Lock()
    watcher._current_ws = None
    watcher._stop_event = threading.Event()
    watcher._msg_id = 0
    watcher._refresh_timer = None
    watcher._refresh_timer_lock = threading.Lock()
    watcher._INITIAL_BACKOFF = initial_backoff  # type: ignore[misc]
    watcher._MAX_BACKOFF = max_backoff  # type: ignore[misc]
    watcher._PING_INTERVAL_S = 0.5  # type: ignore[misc]
    watcher._PING_TIMEOUT_S = 0.5  # type: ignore[misc]
    watcher._MAX_CONSEC_FAILURES = max_consec_failures  # type: ignore[misc]
    watcher._REFRESH_DEBOUNCE_S = 0.3  # type: ignore[misc]
    return watcher


class TestEntityRegistryWatcherMisbehaviourAgainstARealServer:
    """Real protocol misbehaviour, not just the vanilla handshake above --
    each of these is a failure mode HA's real WebSocket API can genuinely
    produce (a rejected token, a dropped connection, a hung server) that a
    mocked `websocket.create_connection` has no way to exhibit, because a
    mock only ever fails in the exact shape a test tells it to."""

    def test_auth_rejected_does_not_populate_the_map_and_does_not_hang(self) -> None:
        def handler(sock: socket.socket) -> None:
            _ws_send_text(sock, json.dumps({"type": "auth_required"}))
            _ws_recv_text(sock)
            _ws_send_text(sock, json.dumps({"type": "auth_invalid", "message": "bad token"}))

        from nibe_ha_integration import HAEntityRegistryWatcher

        stub = _StubSupervisor(ws_handler=handler)
        try:
            watcher = HAEntityRegistryWatcher.__new__(HAEntityRegistryWatcher)
            watcher._registry_map_lock = threading.Lock()
            watcher._unique_id_map = {}
            with _redirect_supervisor_to(stub.port), _supervisor_token():
                start = time.monotonic()
                watcher.refresh_registry()
                elapsed = time.monotonic() - start
            assert watcher._unique_id_map == {}
            assert elapsed < 5.0, "refresh_registry() hung instead of failing fast on bad auth"
        finally:
            stub.close()

    def test_connection_dropped_mid_handshake_does_not_hang(self) -> None:
        """The server accepts the WS upgrade, sends auth_required, then
        vanishes before ever sending auth_ok -- a real "Supervisor
        restarting mid-connect" scenario."""

        def handler(sock: socket.socket) -> None:
            _ws_send_text(sock, json.dumps({"type": "auth_required"}))
            _ws_recv_text(sock)
            sock.close()

        from nibe_ha_integration import HAEntityRegistryWatcher

        stub = _StubSupervisor(ws_handler=handler)
        try:
            watcher = HAEntityRegistryWatcher.__new__(HAEntityRegistryWatcher)
            watcher._registry_map_lock = threading.Lock()
            watcher._unique_id_map = {}
            with _redirect_supervisor_to(stub.port), _supervisor_token():
                start = time.monotonic()
                watcher.refresh_registry()
                elapsed = time.monotonic() - start
            assert watcher._unique_id_map == {}
            assert elapsed < 5.0, "refresh_registry() hung on a connection dropped mid-handshake"
        finally:
            stub.close()

    def test_malformed_greeting_is_rejected_not_a_crash(self) -> None:
        """The very first message is not what the HA protocol promises --
        proves _ws_authenticate's type-check actually guards against a
        genuinely unexpected server response, not just a mocked one."""

        def handler(sock: socket.socket) -> None:
            _ws_send_text(sock, json.dumps({"type": "something_else"}))

        from nibe_ha_integration import HAEntityRegistryWatcher

        stub = _StubSupervisor(ws_handler=handler)
        try:
            watcher = HAEntityRegistryWatcher.__new__(HAEntityRegistryWatcher)
            watcher._registry_map_lock = threading.Lock()
            watcher._unique_id_map = {}
            with _redirect_supervisor_to(stub.port), _supervisor_token():
                watcher.refresh_registry()  # must not raise
            assert watcher._unique_id_map == {}
        finally:
            stub.close()

    def test_run_loop_reconnects_after_a_missing_pong(self) -> None:
        """The long-lived _run() loop (not refresh_registry()'s one-shot
        connection) must notice a connection that stops responding to
        keepalive pings and reconnect -- proven against a real server that
        genuinely goes silent, with the production ping/pong timing
        constants shortened just for this test so it does not take the
        real 30s/15s budget to observe."""
        connect_count = [0]

        def handler(sock: socket.socket) -> None:
            connect_count[0] += 1
            _run_loop_handshake(sock)
            # First connection: go completely silent (no pong) until the
            # client's shortened recv timeout fires and it gives up on us.
            # Second connection (the reconnect): just hang open so the test
            # can observe reconnection happened without needing a third.
            if connect_count[0] == 1:
                time.sleep(2.0)
                with contextlib.suppress(OSError):
                    sock.close()
            else:
                time.sleep(5.0)

        stub = _StubSupervisor(ws_handler=handler)
        try:
            watcher = _make_run_loop_watcher()

            with _redirect_supervisor_to(stub.port), _supervisor_token():
                thread = threading.Thread(target=watcher._run, daemon=True)
                thread.start()
                deadline = time.monotonic() + 8.0
                while connect_count[0] < 2 and time.monotonic() < deadline:
                    time.sleep(0.1)
                watcher._stop_event.set()
                thread.join(timeout=5)

            assert connect_count[0] >= 2, (
                f"Watcher never reconnected after the connection went silent "
                f"(connect_count={connect_count[0]})"
            )
        finally:
            stub.close()


class TestWebSocketUpgradeRejectedAgainstARealServer:
    """A real, observed-in-production failure mode: HA Core mid-restart
    answers the WebSocket upgrade GET with a genuine non-101 HTTP response
    (502 Bad Gateway, via the Supervisor's own reverse proxy) rather than
    ever completing the handshake -- distinct from every other misbehaviour
    tested above, which all happen *after* a successful 101 Switching
    Protocols response (auth rejected, dropped mid-handshake, malformed
    greeting). websocket-client raises WebSocketBadStatusException for
    this, a case _run()'s reconnect loop had never actually been proven
    against a real HTTP 502 response before -- only reasoned about via the
    same broad `except Exception` that also happens to catch it."""

    def test_repeated_502_during_upgrade_backs_off_then_recovers(self) -> None:
        connect_count = [0]

        def handler(sock: socket.socket) -> None:
            connect_count[0] += 1
            _run_loop_handshake(sock)
            time.sleep(3.0)

        # First 3 upgrade attempts get a real 502; the 4th (and onward)
        # actually upgrades and completes the handshake.
        stub = _StubSupervisor(ws_handler=handler, bad_status_count=3)
        try:
            watcher = _make_run_loop_watcher()
            with _redirect_supervisor_to(stub.port), _supervisor_token():
                thread = threading.Thread(target=watcher._run, daemon=True)
                thread.start()
                deadline = time.monotonic() + 15.0
                while connect_count[0] < 1 and time.monotonic() < deadline:
                    time.sleep(0.1)
                watcher._stop_event.set()
                thread.join(timeout=5)

            assert stub._upgrade_attempts >= 4, (
                f"Expected at least 3 rejected upgrade attempts plus one that "
                f"succeeded, got {stub._upgrade_attempts} total attempts"
            )
            assert connect_count[0] >= 1, (
                "The watcher never got past the repeated 502s to a real "
                "successful WebSocket upgrade"
            )
        finally:
            stub.close()


class TestProtocolLevelPingPongAgainstARealServer:
    """The keepalive tested above (test_run_loop_reconnects_after_a_missing_pong)
    is HA's own app-level {"type": "ping"}/{"type": "pong"} JSON messages.
    That is a different mechanism from a real WebSocket *protocol*-level
    ping/pong (opcode 0x9/0xA, RFC 6455 §5.5.2-3) -- something the
    underlying websocket-client library, not this project's own code, is
    responsible for answering transparently. A server (or an intermediate
    proxy) is free to send one of these at any time; if the library's
    handling of it ever broke or got disabled, ws.recv() would either raise
    or silently swallow the next real event, and nothing in the JSON-level
    tests above would catch it. This proves a real protocol-level ping from
    the server gets a real protocol-level pong back on the wire, and that
    the app-level event stream is unaffected by it."""

    def test_server_ping_frame_gets_a_real_pong_reply_and_events_still_flow(self) -> None:
        pong_received = threading.Event()
        event_forwarded = threading.Event()

        def handler(sock: socket.socket) -> None:
            sub_id = _run_loop_handshake(sock)
            assert sub_id is not None
            # A real RFC 6455 ping frame, unsolicited, mid-stream.
            _ws_send(sock, b"keepalive", opcode=0x9)
            opcode, payload = _ws_recv(sock)
            if opcode == 0xA and payload == b"keepalive":
                pong_received.set()
            # Confirm the connection is still usable afterwards: a real
            # subscribed event should still be delivered normally.
            _ws_send_text(
                sock,
                json.dumps(
                    {
                        "id": sub_id,
                        "type": "event",
                        "event": {
                            "event_type": "entity_registry_updated",
                            "data": {"action": "update", "entity_id": "sensor.nibe_100"},
                        },
                    }
                ),
            )
            time.sleep(3.0)

        stub = _StubSupervisor(ws_handler=handler)
        try:
            watcher = _make_run_loop_watcher()
            original_refresh = watcher.refresh_registry
            watcher._registry_map_lock = threading.Lock()
            watcher._unique_id_map = {}

            def _tracking_refresh() -> None:
                event_forwarded.set()

            watcher.refresh_registry = _tracking_refresh  # type: ignore[method-assign]
            with _redirect_supervisor_to(stub.port), _supervisor_token():
                thread = threading.Thread(target=watcher._run, daemon=True)
                thread.start()
                assert pong_received.wait(timeout=8.0), (
                    "No protocol-level pong frame was ever sent back for the "
                    "server's ping -- the client's ping/pong handling broke"
                )
                assert event_forwarded.wait(timeout=8.0), (
                    "A real event sent right after the ping/pong exchange was "
                    "never processed -- the ping frame disrupted the stream"
                )
                watcher._stop_event.set()
                thread.join(timeout=5)
            watcher.refresh_registry = original_refresh
        finally:
            stub.close()


class TestReconnectRacingDebouncedRefreshAgainstARealServer:
    """_registry_map_lock's own docstring (see HAEntityRegistryWatcher.__init__
    in nibe_ha_integration.py) describes exactly this race: _connect_and_subscribe
    reassigns _unique_id_map wholesale on every (re)connect, while a debounced
    refresh_registry() call -- fired from a Timer thread, over its own,
    separate WebSocket connection -- mutates the same dict in place. That
    race has only ever been reasoned about, never actually produced against
    two real, concurrent connections to a real (stub) server. This forces
    it: an entity_registry_updated event schedules a debounced refresh right
    before the long-lived connection is dropped and forced to reconnect, so
    refresh_registry()'s own connection and the reconnect's fresh registry
    fetch are genuinely in flight against the server at the same time."""

    def test_no_crash_and_a_consistent_final_map_survives_the_race(self) -> None:
        run_connections = [0]
        refresh_connections = [0]
        errors: list[BaseException] = []

        def handler(sock: socket.socket) -> None:
            try:
                _ws_send_text(sock, json.dumps({"type": "auth_required"}))
                _ws_recv_text(sock)
                _ws_send_text(sock, json.dumps({"type": "auth_ok"}))
                first = json.loads(_ws_recv_text(sock))

                if first["type"] == "config/entity_registry/list":
                    # A standalone refresh_registry() connection.
                    refresh_connections[0] += 1
                    _ws_send_text(
                        sock,
                        json.dumps(
                            {
                                "id": first["id"],
                                "type": "result",
                                "success": True,
                                "result": [
                                    {"unique_id": "nibe_100", "entity_id": "sensor.nibe_100"},
                                ],
                            }
                        ),
                    )
                    return

                # The long-lived _run() connection's subscribe_events.
                run_connections[0] += 1
                _ws_send_text(
                    sock, json.dumps({"id": first["id"], "type": "result", "success": True})
                )
                reg_req = json.loads(_ws_recv_text(sock))
                if run_connections[0] == 1:
                    # First connect's own initial registry fetch: empty.
                    _ws_send_text(
                        sock,
                        json.dumps(
                            {"id": reg_req["id"], "type": "result", "success": True, "result": []}
                        ),
                    )
                    # Push a real event, which schedules a debounced
                    # refresh_registry() on its own separate connection
                    # (handled by the branch above), then immediately sever
                    # this connection -- forcing _run() to reconnect while
                    # that debounced refresh is still in flight.
                    _ws_send_text(
                        sock,
                        json.dumps(
                            {
                                "id": first["id"],
                                "type": "event",
                                "event": {
                                    "event_type": "entity_registry_updated",
                                    "data": {
                                        "action": "update",
                                        "entity_id": "sensor.nibe_999",
                                    },
                                },
                            }
                        ),
                    )
                    time.sleep(0.1)
                    sock.close()
                else:
                    # The reconnect: its own fresh registry fetch, racing
                    # the refresh_registry() connection above.
                    _ws_send_text(
                        sock,
                        json.dumps(
                            {
                                "id": reg_req["id"],
                                "type": "result",
                                "success": True,
                                "result": [
                                    {"unique_id": "nibe_200", "entity_id": "sensor.nibe_200"},
                                ],
                            }
                        ),
                    )
                    time.sleep(3.0)
            except (ConnectionError, OSError):
                pass
            except BaseException as e:  # noqa: BLE001 — captured for the test's own assertion
                errors.append(e)

        stub = _StubSupervisor(ws_handler=handler)
        try:
            watcher = _make_run_loop_watcher()
            with _redirect_supervisor_to(stub.port), _supervisor_token():
                thread = threading.Thread(target=watcher._run, daemon=True)
                thread.start()
                deadline = time.monotonic() + 10.0
                while (
                    run_connections[0] < 2 or refresh_connections[0] < 1
                ) and time.monotonic() < deadline:
                    time.sleep(0.05)
                # Give the lock-guarded writes on both sides a moment to
                # actually land before inspecting the shared map.
                time.sleep(0.3)
                watcher._stop_event.set()
                thread.join(timeout=5)

            assert errors == [], f"Server-side handler raised: {errors}"
            assert run_connections[0] >= 2, "The watcher never reconnected"
            assert refresh_connections[0] >= 1, "The debounced refresh_registry() never ran"
            # The reconnect's wholesale reassignment is the last write to
            # settle in every timing this test has produced -- its entry
            # must survive. Whether nibe_100 (the earlier, racing refresh)
            # also survives depends on exact scheduling and is not asserted
            # -- the real guarantee under test is no crash and no dict
            # corruption, not a specific winner.
            assert isinstance(watcher._unique_id_map, dict)
            assert watcher.entity_id_for(200) == "sensor.nibe_200"
        finally:
            stub.close()


class TestNotifyDismissMisbehaviourAgainstARealServer:
    """notify_ha/dismiss_ha are called from other exception handlers and
    must never themselves raise -- proven here against real failures
    (a real 500, a real refused connection), not a mocked urlopen that only
    ever fails in the shape a test tells it to."""

    def test_notify_ha_does_not_raise_on_a_real_500(self) -> None:
        from nibe_ha_integration import notify_ha

        class _FailHandler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                self.send_response(500)
                self.end_headers()

        server = _HAStubServer(("127.0.0.1", 0), _FailHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with _redirect_supervisor_to(server.server_address[1]), _supervisor_token():
                notify_ha(None, "Title", "Message", "notif_id")  # must not raise
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_dismiss_ha_does_not_raise_on_connection_refused(self) -> None:
        from nibe_ha_integration import dismiss_ha

        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
        probe.close()

        with _redirect_supervisor_to(closed_port), _supervisor_token():
            dismiss_ha(None, "notif_id")  # must not raise

    def test_notify_ha_times_out_and_returns_instead_of_hanging_forever(self) -> None:
        """A Supervisor that accepts the TCP connection and then never
        answers (hung, not down) is a real, distinct failure mode from
        connection-refused above -- it can only be told apart from a slow
        but working server by the request's own timeout actually firing.
        Proves urlopen's real timeout=10 kwarg is honoured over a real
        socket, not just present in the call, and that notify_ha still
        returns (doesn't hang the caller) once it does."""
        from nibe_ha_integration import notify_ha

        accepted = threading.Event()

        class _HangingHandler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                accepted.set()
                # Read the request body so the client's send completes, then
                # simply never call send_response()/end_headers() -- the
                # connection stays open and silent until the client's own
                # read timeout gives up on it.
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length:
                    self.rfile.read(length)
                time.sleep(15.0)

        server = _HAStubServer(("127.0.0.1", 0), _HangingHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with _redirect_supervisor_to(server.server_address[1]), _supervisor_token():
                start = time.monotonic()
                notify_ha(None, "Title", "Message", "notif_id")  # must not raise
                elapsed = time.monotonic() - start
            assert accepted.wait(timeout=1.0), "The stub server never received the request"
            assert elapsed < 14.0, (
                f"notify_ha took {elapsed:.1f}s -- longer than its own 10s "
                f"timeout plus margin, meaning it did not actually time out"
            )
            assert elapsed >= 9.0, (
                f"notify_ha returned after only {elapsed:.1f}s -- suspiciously "
                f"fast for a genuine 10s socket timeout, check the server "
                f"really withheld its response"
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)


class TestSubscribeEventsFailureAgainstARealServer:
    def test_failed_subscription_triggers_reconnect_not_a_hang(self) -> None:
        """A real subscribe_events rejection (success: false) -- the
        connect_and_subscribe() path raises RuntimeError on this, which
        _run() must treat like any other connection failure: reconnect,
        not get stuck."""
        connect_count = [0]

        def handler(sock: socket.socket) -> None:
            connect_count[0] += 1
            if connect_count[0] == 1:
                _run_loop_handshake(sock, subscribe_success=False)
            else:
                _run_loop_handshake(sock)
                time.sleep(5.0)

        stub = _StubSupervisor(ws_handler=handler)
        try:
            watcher = _make_run_loop_watcher()
            with _redirect_supervisor_to(stub.port), _supervisor_token():
                thread = threading.Thread(target=watcher._run, daemon=True)
                thread.start()
                deadline = time.monotonic() + 8.0
                while connect_count[0] < 2 and time.monotonic() < deadline:
                    time.sleep(0.1)
                watcher._stop_event.set()
                thread.join(timeout=5)

            assert connect_count[0] >= 2, (
                f"Watcher never reconnected after a rejected subscription "
                f"(connect_count={connect_count[0]})"
            )
        finally:
            stub.close()


class TestGiveUpAfterMaxConsecutiveFailuresAgainstARealServer:
    def test_run_loop_stops_retrying_after_the_failure_ceiling(self) -> None:
        """The watcher must eventually give up rather than retry forever
        against a Supervisor that is permanently unreachable -- proven by
        actually exhausting the (shortened, for test speed) failure
        ceiling against a real closed port, not by inspecting the loop's
        source for a counter that might never really be reached."""
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
        probe.close()

        watcher = _make_run_loop_watcher(
            initial_backoff=0.05, max_backoff=0.1, max_consec_failures=3
        )
        with _redirect_supervisor_to(closed_port), _supervisor_token():
            thread = threading.Thread(target=watcher._run, daemon=True)
            thread.start()
            # No connection attempt can ever succeed against a closed port,
            # so the thread must exit entirely on its own once the failure
            # ceiling trips -- it must not need _stop_event set to stop.
            thread.join(timeout=10)

        assert not thread.is_alive(), (
            "Registry watcher never gave up against a permanently unreachable "
            "Supervisor -- it would retry forever in production"
        )


class TestMalformedFrameMidStreamAgainstARealServer:
    def test_garbage_frame_is_discarded_the_loop_keeps_running(self) -> None:
        """A single non-JSON text frame arriving mid-stream (real firmware/
        proxy misbehaviour, not something a mocked recv() would ever
        produce on its own) must be discarded, not kill the connection --
        proven by sending one, then a genuine event, and confirming the
        genuine event still gets processed."""

        def handler(sock: socket.socket) -> None:
            sub_id = _run_loop_handshake(sock)
            _ws_send_text(sock, "this is not valid json{{{")
            _ws_send_text(
                sock,
                json.dumps(
                    {
                        "id": sub_id,
                        "type": "event",
                        "event": {
                            "data": {
                                "action": "create",
                                "entity_id": "sensor.nibe_999",
                                "unique_id": "nibe_999",
                            }
                        },
                    }
                ),
            )
            time.sleep(2.0)

        stub = _StubSupervisor(ws_handler=handler)
        try:
            watcher = _make_run_loop_watcher()
            with _redirect_supervisor_to(stub.port), _supervisor_token():
                thread = threading.Thread(target=watcher._run, daemon=True)
                thread.start()
                deadline = time.monotonic() + 5.0
                while watcher.entity_id_for(999) is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                watcher._stop_event.set()
                thread.join(timeout=5)

            assert watcher.entity_id_for(999) == "sensor.nibe_999", (
                "The genuine event after the garbage frame was never processed "
                "-- the malformed frame likely killed the connection"
            )
        finally:
            stub.close()


# ============================================================================
# Lovelace provisioning (nibe_lovelace.py) -- a separate HA Supervisor
# WebSocket consumer from HAEntityRegistryWatcher above, with its own
# (duplicated) auth handshake and over a dozen distinct lovelace/* commands.
# Rather than stub every command's own request/response shape, this suite
# targets _ws_call() -- the single primitive every one of those commands
# goes through -- plus one full real end-to-end flow, so the shared risk
# across all call sites is covered without a bespoke test per command.
# ============================================================================


def _open_ws_to_stub(port: int, *, authenticate: bool = False):
    """A raw websocket-client connection to the stub, bypassing
    nibe_lovelace's own _open_ha_websocket() -- _ws_call() itself doesn't
    care whether auth already happened, so tests targeting it directly
    don't need a full handshake in the way."""
    import websocket

    with _redirect_supervisor_to(port):
        ws = websocket.create_connection("ws://supervisor/core/websocket", timeout=5)
    if authenticate:
        _ws_recv_text_client(ws)  # auth_required
        ws.send(json.dumps({"type": "auth", "access_token": "test-token"}))
        _ws_recv_text_client(ws)  # auth_ok
    return ws


def _ws_recv_text_client(ws: Any) -> str:
    """websocket-client's own .recv(), named distinctly from this module's
    _ws_recv_text (which reads server-side off a raw accepted socket) so
    it's clear which side of the connection each helper operates on."""
    result: str = ws.recv()
    return result


class TestWsCallAgainstARealServer:
    """_ws_call() is the shared primitive every lovelace/* command in
    nibe_lovelace.py goes through -- proving its id-matching, cumulative
    timeout budget, and error handling against a real server covers all
    fifteen-plus call sites at once, not just one command's happy path."""

    def test_matches_by_id_and_skips_irrelevant_messages(self) -> None:
        from nibe_lovelace import _ws_call

        def handler(sock: socket.socket) -> None:
            req = json.loads(_ws_recv_text(sock))
            # Noise a real HA connection can genuinely interleave: a result
            # for a different in-flight call, and a pushed event.
            _ws_send_text(sock, json.dumps({"id": 999, "type": "result", "success": True}))
            _ws_send_text(sock, json.dumps({"type": "event", "event": {}}))
            _ws_send_text(
                sock,
                json.dumps(
                    {"id": req["id"], "type": "result", "success": True, "result": {"ok": 1}}
                ),
            )

        stub = _StubSupervisor(ws_handler=handler)
        try:
            ws = _open_ws_to_stub(stub.port)
            try:
                resp = _ws_call(ws, 42, {"type": "test_call"}, timeout=5)
                assert resp.get("result") == {"ok": 1}
            finally:
                ws.close()
        finally:
            stub.close()

    def test_overall_timeout_budget_is_cumulative_not_per_recv(self) -> None:
        """A stream of irrelevant messages, each arriving well within its
        own recv() window, must not let the total wait exceed `timeout` --
        proves the deadline is real wall-clock time, not reset by every
        recv() call the way a naive `ws.settimeout(timeout)` once before
        the loop would allow."""
        from nibe_lovelace import _ws_call

        def handler(sock: socket.socket) -> None:
            _ws_recv_text(sock)
            for _ in range(20):
                time.sleep(0.1)
                _ws_send_text(sock, json.dumps({"id": 999, "type": "result", "success": True}))

        stub = _StubSupervisor(ws_handler=handler)
        try:
            ws = _open_ws_to_stub(stub.port)
            try:
                start = time.monotonic()
                resp = _ws_call(ws, 42, {"type": "test_call"}, timeout=1.0)
                elapsed = time.monotonic() - start
                assert resp == {}
                assert elapsed < 2.0, (
                    f"_ws_call took {elapsed:.1f}s against a 1.0s timeout -- the "
                    f"budget is being reset per-recv instead of held cumulatively"
                )
            finally:
                ws.close()
        finally:
            stub.close()

    def test_send_failure_returns_empty_dict_not_a_raise(self) -> None:
        from nibe_lovelace import _ws_call

        stub = _StubSupervisor(ws_handler=lambda sock: None)
        try:
            ws = _open_ws_to_stub(stub.port)
            ws.close()  # send() on an already-closed real socket fails for real
            assert _ws_call(ws, 1, {"type": "test_call"}, timeout=2) == {}
        finally:
            stub.close()

    def test_malformed_json_response_returns_empty_dict_not_a_crash(self) -> None:
        from nibe_lovelace import _ws_call

        def handler(sock: socket.socket) -> None:
            _ws_recv_text(sock)
            _ws_send_text(sock, "not valid json{{{")

        stub = _StubSupervisor(ws_handler=handler)
        try:
            ws = _open_ws_to_stub(stub.port)
            try:
                assert _ws_call(ws, 1, {"type": "test_call"}, timeout=2) == {}
            finally:
                ws.close()
        finally:
            stub.close()

    def test_connection_closed_before_any_response_returns_empty_dict(self) -> None:
        from nibe_lovelace import _ws_call

        def handler(sock: socket.socket) -> None:
            _ws_recv_text(sock)
            sock.close()

        stub = _StubSupervisor(ws_handler=handler)
        try:
            ws = _open_ws_to_stub(stub.port)
            try:
                assert _ws_call(ws, 1, {"type": "test_call"}, timeout=2) == {}
            finally:
                with contextlib.suppress(OSError):
                    ws.close()
        finally:
            stub.close()


class TestLovelaceProvisioningAgainstARealServer:
    def test_setup_lovelace_resource_end_to_end_creates_a_new_resource(self) -> None:
        """The full real stack: _open_ha_websocket()'s own auth handshake,
        then _setup_lovelace_resource()'s real list -> (no match) -> create
        sequence, each hop going through the real _ws_call() above."""

        def handler(sock: socket.socket) -> None:
            _ws_send_text(sock, json.dumps({"type": "auth_required"}))
            _ws_recv_text(sock)
            _ws_send_text(sock, json.dumps({"type": "auth_ok"}))

            list_req = json.loads(_ws_recv_text(sock))
            assert list_req["type"] == "lovelace/resources/list"
            _ws_send_text(
                sock,
                json.dumps({"id": list_req["id"], "type": "result", "success": True, "result": []}),
            )

            create_req = json.loads(_ws_recv_text(sock))
            assert create_req["type"] == "lovelace/resources/create"
            assert create_req["url"] == "/local/nibe-entity-manager-card.js?v=1"
            _ws_send_text(
                sock,
                json.dumps(
                    {"id": create_req["id"], "type": "result", "success": True, "result": {}}
                ),
            )

        stub = _StubSupervisor(ws_handler=handler)
        try:
            from nibe_lovelace import _open_ha_websocket, _setup_lovelace_resource

            with _redirect_supervisor_to(stub.port), _supervisor_token():
                opened = _open_ha_websocket()
                assert opened is not None, "Auth handshake against the real stub failed"
                ws, next_id = opened
                try:
                    _setup_lovelace_resource(ws, next_id, "/local/nibe-entity-manager-card.js?v=1")
                finally:
                    ws.close()
        finally:
            stub.close()

    def test_open_ha_websocket_returns_none_on_auth_rejection_not_a_raise(self) -> None:
        def handler(sock: socket.socket) -> None:
            _ws_send_text(sock, json.dumps({"type": "auth_required"}))
            _ws_recv_text(sock)
            _ws_send_text(sock, json.dumps({"type": "auth_invalid", "message": "bad token"}))

        stub = _StubSupervisor(ws_handler=handler)
        try:
            from nibe_lovelace import _open_ha_websocket

            with _redirect_supervisor_to(stub.port), _supervisor_token():
                assert _open_ha_websocket() is None
        finally:
            stub.close()


class TestGracefulCloseFrameAgainstARealServer:
    def test_real_close_frame_is_treated_like_any_other_disconnect(self) -> None:
        """A graceful, intentional server-side close (a real WS close
        frame, opcode 0x8 -- e.g. Supervisor restarting cleanly) is a
        different point in websocket-client's own receive state machine
        than the raw severed-socket tests elsewhere in this file (which
        simulate a crash with no closing handshake at all). Both must be
        survived the same way: no hang, no crash, empty map."""

        def handler(sock: socket.socket) -> None:
            _ws_send_text(sock, json.dumps({"type": "auth_required"}))
            _ws_recv_text(sock)
            _ws_send_text(sock, json.dumps({"type": "auth_ok"}))
            _ws_send_close(sock, code=1001, reason="restarting")

        from nibe_ha_integration import HAEntityRegistryWatcher

        stub = _StubSupervisor(ws_handler=handler)
        try:
            watcher = HAEntityRegistryWatcher.__new__(HAEntityRegistryWatcher)
            watcher._registry_map_lock = threading.Lock()
            watcher._unique_id_map = {}
            with _redirect_supervisor_to(stub.port), _supervisor_token():
                start = time.monotonic()
                watcher.refresh_registry()
                elapsed = time.monotonic() - start
            assert watcher._unique_id_map == {}
            assert elapsed < 5.0, "A real close frame caused a hang instead of a clean return"
        finally:
            stub.close()


class TestUnrecognizedRegistryActionAgainstARealServer:
    def test_unknown_event_action_does_not_break_the_loop(self) -> None:
        """A real HA registry event with an action this code doesn't
        explicitly handle (only create/update/remove are known) -- future
        HA versions could add new actions, and the loop must tolerate one
        rather than crash or desync, proven by confirming a *subsequent*
        genuine event still gets processed correctly afterward."""

        def handler(sock: socket.socket) -> None:
            sub_id = _run_loop_handshake(sock)
            _ws_send_text(
                sock,
                json.dumps(
                    {
                        "id": sub_id,
                        "type": "event",
                        "event": {"data": {"action": "move", "entity_id": "sensor.moved"}},
                    }
                ),
            )
            _ws_send_text(
                sock,
                json.dumps(
                    {
                        "id": sub_id,
                        "type": "event",
                        "event": {
                            "data": {
                                "action": "create",
                                "entity_id": "sensor.nibe_998",
                                "unique_id": "nibe_998",
                            }
                        },
                    }
                ),
            )
            time.sleep(2.0)

        stub = _StubSupervisor(ws_handler=handler)
        try:
            watcher = _make_run_loop_watcher()
            with _redirect_supervisor_to(stub.port), _supervisor_token():
                thread = threading.Thread(target=watcher._run, daemon=True)
                thread.start()
                deadline = time.monotonic() + 5.0
                while watcher.entity_id_for(998) is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                watcher._stop_event.set()
                thread.join(timeout=5)

            assert watcher.entity_id_for(998) == "sensor.nibe_998", (
                "The genuine event after the unrecognized action was never "
                "processed -- the unknown action likely broke the loop"
            )
        finally:
            stub.close()


class TestEventBurstDebounceAgainstARealServer:
    def test_burst_of_create_events_triggers_exactly_one_refresh(self) -> None:
        """The debounce this exists for (see _schedule_refresh_registry's
        own docstring): a burst of "create" events lacking a unique_id --
        normal for essentially every newly created MQTT entity -- must
        coalesce into exactly one refresh_registry() WebSocket connection,
        not one per event. Proven by actually firing a real burst and
        counting real connections to a real server, not by inspecting the
        debounce timer's source for a cancel-and-reschedule pattern that
        might not actually behave that way under real concurrent load."""
        from nibe_ha_integration import HAEntityRegistryWatcher

        connect_count = [0]

        def post_auth(sock: socket.socket) -> None:
            connect_count[0] += 1
            req = json.loads(_ws_recv_text(sock))
            _ws_send_text(
                sock,
                json.dumps({"id": req["id"], "type": "result", "success": True, "result": []}),
            )

        stub = _StubSupervisor(ws_handler=_auth_ok_handler(post_auth))
        try:
            watcher = HAEntityRegistryWatcher.__new__(HAEntityRegistryWatcher)
            watcher._registry_map_lock = threading.Lock()
            watcher._unique_id_map = {}
            watcher._refresh_timer = None
            watcher._refresh_timer_lock = threading.Lock()
            watcher._REFRESH_DEBOUNCE_S = 0.3  # type: ignore[misc]

            with _redirect_supervisor_to(stub.port), _supervisor_token():
                for i in range(10):
                    watcher._handle_event(
                        {"data": {"action": "create", "entity_id": f"sensor.burst_{i}"}}
                    )
                    time.sleep(0.02)
                # Past the (shortened) debounce window, so the single
                # coalesced refresh has had time to actually fire and
                # complete its real connection to the stub.
                time.sleep(1.5)

            assert connect_count[0] == 1, (
                f"Expected the burst of 10 events to coalesce into exactly "
                f"1 refresh_registry() connection, got {connect_count[0]}"
            )
        finally:
            stub.close()
