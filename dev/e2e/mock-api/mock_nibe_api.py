"""
mock_nibe_api.py
=================
Minimal HTTPS mock of the Nibe SMO S40 local REST API, for the dev/e2e
harness only. Not part of the production bridge or the pytest suite.

Serves the exact endpoint shapes NibeApiClient (app/nibe_api.py) expects,
replaying real firmware data from reference-dumps/all_points_<lang>.json:

  GET  /api/v1/devices/0                → device root / product info
  GET  /api/v1/devices/0/points         → bulk points dict {id: point}
  GET  /api/v1/devices/0/points/{id}    → single point, 404 if absent
  PATCH /api/v1/devices/0/points        → accepts writes, updates in-memory state
  GET  /api/v1/devices/0/notifications  → {"alarms": []}
  DELETE /api/v1/devices/0/notifications → 204

  POST /mock-control/points/{id}        → test-only control channel (see below)

Any HTTP Basic Authorization header is accepted (this is a dev harness, not
an auth test). Runs over HTTPS with a generated self-signed certificate,
since NibeApiClient always connects over TLS.

The /mock-control/points/{id} endpoint is not part of the real Nibe API
surface — it exists purely so the e2e harness's own tests can simulate a
firmware value change across polls (e.g. to exercise dynamic binary_sensor
reclassification: a point starts out reporting a boolean 0/1 value, and a
later poll needs to see it report something else). It unconditionally
overwrites a point's value.integerValue/stringValue, bypassing the
isWritable check the real PATCH .../points endpoint enforces — this
simulates the device itself changing the value, not an HA-side write.
Body: {"integerValue": <int>} and/or {"stringValue": <str>}.
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DUMP_PATH = os.environ.get("MOCK_NIBE_DUMP", "/data/all_points_en.json")
DEVICE_ID = "0"
BASE_PATH = f"/api/v1/devices/{DEVICE_ID}"

with open(DUMP_PATH, encoding="utf-8") as f:
    POINTS: dict[str, dict] = json.load(f)

DEVICE_ROOT = {
    "product": {
        "name": "SMO S40",
        "manufacturer": "NIBE",
        "firmwareId": "8310",
        "serialNumber": "0000000000000000",
    },
    "connectionState": "ONLINE",
}


def _ensure_cert(cert_path: str, key_path: str) -> None:
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            key_path,
            "-out",
            cert_path,
            "-days",
            "3650",
            "-nodes",
            "-subj",
            "/CN=mock-nibe-api",
        ],
        check=True,
    )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: D401 — keep container logs quiet
        sys.stderr.write("mock-nibe-api: " + (fmt % args) + "\n")

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 — stdlib method name
        path = self.path.split("?", 1)[0]
        if path == BASE_PATH or path == BASE_PATH + "/":
            self._send_json(200, DEVICE_ROOT)
        elif path == f"{BASE_PATH}/points":
            self._send_json(200, POINTS)
        elif path.startswith(f"{BASE_PATH}/points/"):
            point_id = path.rsplit("/", 1)[-1]
            point = POINTS.get(point_id)
            if point is None:
                self._send_json(404, {"error": "not found"})
            else:
                self._send_json(200, point)
        elif path == f"{BASE_PATH}/notifications":
            self._send_json(200, {"alarms": []})
        else:
            self._send_json(404, {"error": "not found"})

    def do_PATCH(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path != f"{BASE_PATH}/points":
            self._send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"[]"
        try:
            writes = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._send_json(400, {"error": "bad json"})
            return

        result = {}
        for item in writes:
            point_id = str(item.get("variableId"))
            point = POINTS.get(point_id)
            if point is None:
                result[point_id] = "error: no such param"
                continue
            if not point.get("metadata", {}).get("isWritable", False):
                result[point_id] = "error: read only value"
                continue
            point["value"]["integerValue"] = item.get("integerValue")
            point["value"]["stringValue"] = item.get("stringValue")
            point["value"]["isOk"] = True
            result[point_id] = "modified"
        self._send_json(200, result)

    def do_DELETE(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == f"{BASE_PATH}/notifications":
            self.send_response(204)
            self.end_headers()
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        # Test-only control channel — see module docstring. Not part of the
        # real Nibe API; used by dev/e2e/tests/*.spec.ts to simulate a
        # firmware value change across polls without restarting the mock.
        path = self.path.split("?", 1)[0]
        prefix = "/mock-control/points/"
        if not path.startswith(prefix):
            self._send_json(404, {"error": "not found"})
            return
        point_id = path[len(prefix) :]
        point = POINTS.get(point_id)
        if point is None:
            self._send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._send_json(400, {"error": "bad json"})
            return
        if "integerValue" in body:
            point["value"]["integerValue"] = body["integerValue"]
        if "stringValue" in body:
            point["value"]["stringValue"] = body["stringValue"]
        point["value"]["isOk"] = True
        self._send_json(200, {"status": "ok", "point": point})


def main() -> None:
    port = int(os.environ.get("MOCK_NIBE_PORT", "8443"))
    cert_dir = tempfile.mkdtemp(prefix="mock-nibe-tls-")
    cert_path = os.path.join(cert_dir, "cert.pem")
    key_path = os.path.join(cert_dir, "key.pem")
    _ensure_cert(cert_path, key_path)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # nosec B104 - dev harness only
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    print(f"mock-nibe-api: serving {len(POINTS)} points from {DUMP_PATH} on :{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
