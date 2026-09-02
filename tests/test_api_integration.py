"""Integration tests: NibeApiClient against a real HTTP server.

tests/test_api.py proves request() calls urllib with the right arguments,
using a mocked urlopen. This proves the actual retry/error-handling logic
behaves correctly over a real socket and real HTTP framing — a mocked
urlopen can raise urllib.error.HTTPError on command, but it cannot exercise
a real connection reset mid-response, a real 5xx-then-200 retry sequence
decoded from real response bytes, or a real timeout waiting on a socket
that never answers. Those are exactly the failure modes _describe_network_error()
and the retry loop exist to handle.

Runs a plain http.server in a background thread on an ephemeral localhost
port — no external service, no Docker, no opt-in env var. TLS negotiation
itself is deliberately out of scope here (self.ssl_context is simply
ignored by urlopen for a plain http:// URL, same as production code would
skip it) — that side of the client is exercised by nibe_connectivity_check's
own real-curl diagnostic instead; this suite is only about request()'s
retry/error/JSON-decoding behaviour, which is identical over HTTP or HTTPS.
"""

from __future__ import annotations

import json
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any

import pytest
from nibe_api import NibeApiClient


class _QueuedResponse:
    __slots__ = (
        "body",
        "close_after_status_line",
        "close_without_response",
        "delay",
        "headers",
        "status",
    )

    def __init__(
        self,
        status: int = 200,
        body: Any = None,
        delay: float = 0.0,
        close_without_response: bool = False,
        close_after_status_line: bool = False,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.delay = delay
        self.close_without_response = close_without_response
        # A real connection reset while the client is mid-read of the
        # response *headers* -- a distinct urllib code path from a reset
        # mid-body (close_without_response, which accepts the request and
        # sends nothing at all): here the status line has already arrived
        # before the connection dies.
        self.close_after_status_line = close_after_status_line
        self.headers = headers or {}


class _StubServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class _StubNibeDevice:
    """A minimal real HTTP server standing in for the Nibe controller.

    Responses are queued FIFO — each request pops the next queued response
    (or 200 {} if the queue is empty, so tests that don't care about the
    body still get a well-formed response). Every received request is
    recorded so tests can assert on method/path/headers actually sent over
    the wire, not just what NibeApiClient's own code claims to send.
    """

    def __init__(self) -> None:
        self.responses: list[_QueuedResponse] = []
        self.received: list[dict] = []
        self._state_lock = threading.Lock()
        self._active_count = 0
        self.max_concurrent = 0
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _handle(self) -> None:
                with outer._state_lock:
                    outer._active_count += 1
                    outer.max_concurrent = max(outer.max_concurrent, outer._active_count)
                try:
                    length = int(self.headers.get("Content-Length", 0) or 0)
                    body = self.rfile.read(length) if length else b""
                    with outer._state_lock:
                        outer.received.append(
                            {
                                "method": self.command,
                                "path": self.path,
                                "headers": dict(self.headers),
                                "body": body,
                            }
                        )
                        queued = outer.responses.pop(0) if outer.responses else _QueuedResponse()
                    if queued.delay:
                        time.sleep(queued.delay)
                    self._respond(queued)
                finally:
                    with outer._state_lock:
                        outer._active_count -= 1

            def _respond(self, queued: _QueuedResponse) -> None:
                if queued.close_without_response:
                    self.connection.close()
                    return
                if queued.close_after_status_line:
                    # Write only the raw status line, bypassing
                    # send_response()'s automatic Server/Date headers, then
                    # sever the connection before any header the client is
                    # expecting (Content-Length, etc.) ever arrives.
                    self.wfile.write(b"HTTP/1.1 200 OK\r\n")
                    self.wfile.flush()
                    self.connection.close()
                    return
                if queued.status == 204:
                    # RFC 7230: a 204 response must not include a body.
                    # resetNotifications' real documented success response —
                    # tests must not assume every success has a JSON body.
                    self.send_response(204)
                    self.end_headers()
                    return
                payload = (
                    json.dumps(queued.body).encode()
                    if not isinstance(queued.body, (bytes, type(None)))
                    else (queued.body or b"{}")
                )
                self.send_response(queued.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                for key, value in queued.headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:
                self._handle()

            def do_PATCH(self) -> None:
                self._handle()

            def do_POST(self) -> None:
                self._handle()

            def do_DELETE(self) -> None:
                self._handle()

        self._server = _StubServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def queue(self, *responses: _QueuedResponse) -> None:
        self.responses.extend(responses)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class TestTlsCertificateValidationAgainstARealServer:
    """The module docstring above says TLS negotiation is out of scope for
    this file because urlopen ignores ssl_context for plain http:// URLs --
    but that only means the *happy path* (verification disabled, matching
    the default self-signed-friendly context) is untested here. Whether
    self.ssl_context is actually wired into urlopen()'s TLS handshake at
    all -- i.e. whether enabling verification (nibe_ca_cert set) genuinely
    rejects an untrusted cert instead of silently accepting anything -- is
    a real, previously-untested gap. This class closes it against a real
    HTTPS server presenting a real self-signed certificate."""

    def test_verifying_context_rejects_an_untrusted_self_signed_cert(self, tmp_path) -> None:
        import subprocess

        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        subprocess.run(  # nosec B603, B607 — fixed args, throwaway test cert
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "1",
                "-nodes",
                "-subj",
                "/CN=127.0.0.1",
                "-addext",
                "subjectAltName=IP:127.0.0.1",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )

        handler_hit = threading.Event()

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                handler_hit.set()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args: Any) -> None:
                pass

        class _StubServer(ThreadingMixIn, HTTPServer):
            daemon_threads = True

        server = _StubServer(("127.0.0.1", 0), _Handler)
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
        server.socket = server_ctx.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address[:2]
            # A default verifying context (no cafile) will not trust this
            # self-signed cert -- exactly like _build_ssl_context() with
            # nibe_ca_cert pointed at the wrong/missing CA file.
            client = NibeApiClient(
                base_url=f"https://{host}:{port}",
                auth="Basic dGVzdA==",
                ssl_context=ssl.create_default_context(),
            )
            result = client.fetch_device_info()
            assert result is None
            assert client.last_error is not None
            assert not handler_hit.is_set(), (
                "The handler ran, meaning the TLS handshake was NOT rejected "
                "-- ssl_context is not actually being enforced"
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_verifying_context_accepts_a_cert_signed_by_a_trusted_ca(self, tmp_path) -> None:
        import subprocess

        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        subprocess.run(  # nosec B603, B607 — fixed args, throwaway test cert
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "1",
                "-nodes",
                "-subj",
                "/CN=127.0.0.1",
                "-addext",
                "subjectAltName=IP:127.0.0.1",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"product": {"serialNumber": "TLSOK"}}')

            def log_message(self, *args: Any) -> None:
                pass

        class _StubServer(ThreadingMixIn, HTTPServer):
            daemon_threads = True

        server = _StubServer(("127.0.0.1", 0), _Handler)
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
        server.socket = server_ctx.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address[:2]
            # This time the client trusts exactly the CA (self-signed cert
            # doubling as its own CA) that signed the server's cert --
            # mirrors _build_ssl_context()'s cafile=nibe_ca_cert branch.
            trusting_ctx = ssl.create_default_context(cafile=str(cert))
            client = NibeApiClient(
                base_url=f"https://{host}:{port}",
                auth="Basic dGVzdA==",
                ssl_context=trusting_ctx,
            )
            result = client.fetch_device_info()
            assert result == {"product": {"serialNumber": "TLSOK"}}
            assert client.last_error is None
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


@pytest.fixture
def stub_device():
    device = _StubNibeDevice()
    try:
        yield device
    finally:
        device.close()


def _client(device: _StubNibeDevice) -> NibeApiClient:
    return NibeApiClient(
        base_url=device.base_url,
        auth="Basic dGVzdA==",
        ssl_context=ssl.create_default_context(),
    )


class TestSuccessfulRequestsAgainstARealServer:
    def test_fetch_device_info_returns_the_real_decoded_body(self, stub_device) -> None:
        stub_device.queue(
            _QueuedResponse(200, {"product": {"serialNumber": "TEST123"}}),
        )
        client = _client(stub_device)
        result = client.fetch_device_info()
        assert result == {"product": {"serialNumber": "TEST123"}}

    def test_sends_real_auth_and_accept_headers(self, stub_device) -> None:
        stub_device.queue(_QueuedResponse(200, {}))
        client = _client(stub_device)
        client.fetch_device_info()
        assert stub_device.received[0]["headers"]["Authorization"] == "Basic dGVzdA=="
        assert stub_device.received[0]["headers"]["Accept"] == "application/json"

    def test_fetch_bulk_points_hits_the_points_path(self, stub_device) -> None:
        stub_device.queue(_QueuedResponse(200, {"1": {"variableId": 1}}))
        client = _client(stub_device)
        result = client.fetch_bulk_points()
        assert result == {"1": {"variableId": 1}}
        assert stub_device.received[0]["path"] == "/points"


class TestLargeChunkedResponseAgainstARealServer:
    """Every other test's fixture bodies are small, fully-buffered JSON --
    fine for proving request-shape and error-handling, but a real firmware
    with a large point count can plausibly answer /points with a response
    large enough that the device's own HTTP stack sends it
    Transfer-Encoding: chunked rather than a single Content-Length body.
    urllib.request.urlopen handles dechunking itself, but that path was
    never actually exercised here — every stub response so far used
    send_header("Content-Length", ...), never a real chunked transfer.
    This proves a large, genuinely-chunked /points response still decodes
    correctly end to end."""

    def test_a_large_chunked_points_response_decodes_correctly(self) -> None:
        large_points = {str(i): {"variableId": i, "value": i * 2} for i in range(5000)}
        body = json.dumps(large_points).encode()

        class _ChunkedHandler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                # Real chunked framing (RFC 7230 §4.1), split across
                # several chunks of very different sizes rather than one
                # chunk covering the whole body — the smallest chunk is
                # deliberately tiny to also exercise urlopen's handling of
                # a short, oddly-sized chunk mid-stream, not just one large
                # convenient split.
                chunk_sizes = [1, 17, 4096, len(body)]
                offset = 0
                for size in chunk_sizes:
                    if offset >= len(body):
                        break
                    chunk = body[offset : offset + size]
                    offset += len(chunk)
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                if offset < len(body):
                    remaining = body[offset:]
                    self.wfile.write(f"{len(remaining):x}\r\n".encode() + remaining + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")

        class _StubServer(ThreadingMixIn, HTTPServer):
            daemon_threads = True

        server = _StubServer(("127.0.0.1", 0), _ChunkedHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address[:2]
            client = NibeApiClient(
                base_url=f"http://{host}:{port}",
                auth="Basic dGVzdA==",
                ssl_context=ssl.create_default_context(),
            )
            result = client.fetch_bulk_points()
            assert result is not None
            assert len(result) == 5000
            assert result["4999"] == {"variableId": 4999, "value": 9998}
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class TestAuthAndNotFoundErrorsAgainstARealServer:
    def test_401_raises_http_error_and_sets_last_error(self, stub_device) -> None:
        import urllib.error

        stub_device.queue(_QueuedResponse(401, {"error": "unauthorized"}))
        client = _client(stub_device)
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            client.fetch_device_info()
        assert exc_info.value.code == 401
        assert client.last_error is not None
        assert "401" in client.last_error

    def test_401_is_never_retried_only_one_request_sent(self, stub_device) -> None:
        import urllib.error

        stub_device.queue(_QueuedResponse(401, {}))
        client = _client(stub_device)
        with pytest.raises(urllib.error.HTTPError):
            client.fetch_device_info()
        assert len(stub_device.received) == 1

    def test_404_on_fetch_point_returns_none_not_raise(self, stub_device) -> None:
        stub_device.queue(_QueuedResponse(404, {}))
        client = _client(stub_device)
        assert client.fetch_point(9999) is None


class TestRealAuthorizationHeaderValidationAgainstARealServer:
    """TestAuthAndNotFoundErrorsAgainstARealServer above proves the client
    handles a 401 correctly, but stub_device's 401 there is scripted —
    returned regardless of what credentials were actually sent, so it can't
    catch a bug where the wrong header value is sent but happens to still
    get a canned success queued ahead of it in a test. This class runs a
    server that genuinely inspects the real Authorization header value
    byte-for-byte, the way a real Nibe controller would, and only succeeds
    when it matches."""

    def _server(self, expected_auth: str) -> tuple[HTTPServer, threading.Thread]:
        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.headers.get("Authorization") == expected_auth:
                    body = b'{"product": {"serialNumber": "REALAUTH"}}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    body = b'{"error": "unauthorized"}'
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        class _StubServer(ThreadingMixIn, HTTPServer):
            daemon_threads = True

        server = _StubServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def test_correct_credentials_are_actually_accepted_by_a_real_check(self) -> None:
        server, thread = self._server(expected_auth="Basic Y29ycmVjdDpwYXNz")
        try:
            host, port = server.server_address[:2]
            client = NibeApiClient(
                base_url=f"http://{host}:{port}",
                auth="Basic Y29ycmVjdDpwYXNz",
                ssl_context=ssl.create_default_context(),
            )
            assert client.fetch_device_info() == {"product": {"serialNumber": "REALAUTH"}}
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_wrong_credentials_are_actually_rejected_by_a_real_check(self) -> None:
        import urllib.error

        server, thread = self._server(expected_auth="Basic Y29ycmVjdDpwYXNz")
        try:
            host, port = server.server_address[:2]
            client = NibeApiClient(
                base_url=f"http://{host}:{port}",
                auth="Basic d3Jvbmc6Y3JlZHM=",
                ssl_context=ssl.create_default_context(),
            )
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                client.fetch_device_info()
            assert exc_info.value.code == 401
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class TestRetryBehaviourAgainstARealServer:
    def test_500_then_200_succeeds_on_the_retry(self, stub_device) -> None:
        """The real retry path: a genuinely transient 5xx followed by a
        real success response, decoded from real bytes on the second
        real connection — not a second mocked call scripted in advance."""
        stub_device.queue(
            _QueuedResponse(500, {"error": "overloaded"}),
            _QueuedResponse(200, {"product": {"serialNumber": "TEST123"}}),
        )
        client = _client(stub_device)
        result = client.fetch_device_info()
        assert result == {"product": {"serialNumber": "TEST123"}}
        assert len(stub_device.received) == 2

    def test_500_twice_gives_up_and_returns_none(self, stub_device) -> None:
        stub_device.queue(
            _QueuedResponse(500, {}),
            _QueuedResponse(500, {}),
        )
        client = _client(stub_device)
        assert client.fetch_device_info() is None
        assert len(stub_device.received) == 2

    def test_connection_closed_mid_response_is_treated_as_a_network_error(
        self, stub_device
    ) -> None:
        """A real severed connection (not an HTTPError at all) must still
        be caught and retried like any other transient network failure."""
        stub_device.queue(
            _QueuedResponse(close_without_response=True),
            _QueuedResponse(200, {"ok": True}),
        )
        client = _client(stub_device)
        result = client.fetch_device_info()
        assert result == {"ok": True}
        assert client.last_error is None  # cleared by the successful retry

    def test_400_is_never_retried_only_one_request_sent(self, stub_device) -> None:
        """A non-5xx, non-auth error (malformed request) can never change on
        retry — must give up immediately, not waste a jittered delay."""
        stub_device.queue(_QueuedResponse(400, {}))
        client = _client(stub_device)
        assert client.fetch_device_info() is None
        assert len(stub_device.received) == 1


class TestResetNotificationsAgainstARealServer:
    """reset_notifications() bypasses request() entirely with its own
    urlopen call — not exercised by anything above. Per the vendor's Local
    REST API documentation, a successful reset is HTTP 204 with no body;
    204 responses must not be assumed to carry JSON the way every other
    endpoint here does."""

    def test_204_returns_true(self, stub_device) -> None:
        stub_device.queue(_QueuedResponse(204))
        client = _client(stub_device)
        assert client.reset_notifications() is True
        assert stub_device.received[0]["method"] == "DELETE"
        assert stub_device.received[0]["path"] == "/notifications"

    def test_405_not_supported_returns_false(self, stub_device) -> None:
        stub_device.queue(_QueuedResponse(405, {"error": "not supported"}))
        client = _client(stub_device)
        assert client.reset_notifications() is False

    def test_500_internal_error_returns_false(self, stub_device) -> None:
        stub_device.queue(_QueuedResponse(500, {"error": "internal"}))
        client = _client(stub_device)
        assert client.reset_notifications() is False


class TestWriteDeviceModeAgainstARealServer:
    """write_device_mode() also bypasses request() with its own urlopen
    call and its own HTTPError-body-reading logic — real value here is
    proving that body read actually succeeds against a real HTTPError
    raised from a real socket, not a MagicMock standing in for one."""

    def test_200_returns_true_and_sends_real_payload(self, stub_device) -> None:
        stub_device.queue(_QueuedResponse(200, {}))
        client = _client(stub_device)
        assert client.write_device_mode("aidmode", "on") is True
        received = stub_device.received[0]
        assert received["method"] == "POST"
        assert received["path"] == "/aidmode"
        assert json.loads(received["body"]) == {"aidmode": "on"}

    def test_400_bad_input_returns_false(self, stub_device) -> None:
        stub_device.queue(_QueuedResponse(400, {"error": "bad input"}))
        client = _client(stub_device)
        assert client.write_device_mode("smartmode", "away") is False

    def test_403_wrong_device_id_returns_false(self, stub_device) -> None:
        stub_device.queue(_QueuedResponse(403, {"error": "wrong deviceId"}))
        client = _client(stub_device)
        assert client.write_device_mode("aidmode", "off") is False


class TestRealMisbehaviourAgainstARealServer:
    """Genuine misbehaviour classes, not just vanilla happy-path/documented
    error codes — the same distinction drawn for the MQTT broker
    integration suite. Each of these is a real failure mode this device has
    actually been observed to produce (or could produce), not something a
    mocked urlopen would ever expose on its own."""

    def test_concurrent_requests_are_actually_serialized_on_the_wire(self, stub_device) -> None:
        """The whole reason self._lock exists (see request()'s own
        docstring, citing GitHub discussion #2): the real Nibe controller
        has been observed to stop responding under overlapping request
        load. A mocked urlopen can never expose whether the lock actually
        prevents overlap at the real socket level -- multiple mocked calls
        just run through Python with no concept of "in flight at once".
        Here, real threads hit a real server that can only ever report
        max_concurrent == 1 if requests genuinely never overlapped."""
        for _ in range(5):
            stub_device.queue(_QueuedResponse(200, {}, delay=0.1))
        client = _client(stub_device)

        threads = [threading.Thread(target=client.fetch_device_info) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(stub_device.received) == 5
        assert stub_device.max_concurrent == 1, (
            f"Requests overlapped on the real server (max_concurrent="
            f"{stub_device.max_concurrent}) -- self._lock did not serialize them"
        )

    def test_malformed_json_body_is_handled_gracefully_not_a_crash(self, stub_device) -> None:
        """A real firmware misbehaviour class: truncated/corrupt JSON in an
        otherwise-200 response. request()'s broad except clause around
        json.loads() is proven here against real invalid bytes read off a
        real socket, not a mocked json.loads() raising on command."""
        stub_device.queue(_QueuedResponse(200, body=b"{not valid json"))
        client = _client(stub_device)
        assert client.fetch_device_info() is None
        assert client.last_error is not None

    def test_connection_refused_is_retried_then_reported(self) -> None:
        """The device rebooting, or its REST API service not yet up, looks
        like a real connection refusal -- distinct from close_without_response
        above (which accepts the TCP connection, then drops it mid-request).
        Uses a real closed port, not a mocked ConnectionRefusedError."""
        # Bind and immediately release a port so nothing is listening on it
        # -- more reliable than guessing an arbitrary "probably free" port.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
        probe.close()

        client = NibeApiClient(
            base_url=f"http://127.0.0.1:{closed_port}",
            auth="Basic dGVzdA==",
            ssl_context=ssl.create_default_context(),
        )
        assert client.fetch_device_info() is None
        assert client.last_error is not None

    def test_connection_reset_while_reading_headers_is_a_network_error(self, stub_device) -> None:
        """Distinct from close_without_response above (which resets before
        sending anything at all): here the status line has already arrived
        before the connection dies mid-headers -- a different point in
        urllib's own response-parsing state machine, exercised here against
        a real half-sent response rather than a mocked one."""
        stub_device.queue(_QueuedResponse(close_after_status_line=True))
        client = _client(stub_device)
        assert client.fetch_device_info() is None
        assert client.last_error is not None

    def test_redirect_response_is_followed_transparently(self, stub_device) -> None:
        """The device should never redirect in practice, but if firmware
        or a misconfigured proxy in front of it ever did, urllib's default
        opener follows redirects automatically -- proven here against a
        real 302 + Location header + second real request, not an assumption
        about what a mocked urlopen would have done with one."""
        stub_device.queue(
            _QueuedResponse(302, headers={"Location": "/redirected"}),
            _QueuedResponse(200, {"product": {"serialNumber": "REDIRECTED"}}),
        )
        client = _client(stub_device)
        result = client.fetch_device_info()
        assert result == {"product": {"serialNumber": "REDIRECTED"}}
        assert len(stub_device.received) == 2
        assert stub_device.received[1]["path"] == "/redirected"
