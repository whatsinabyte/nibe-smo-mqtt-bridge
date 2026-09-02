#!/usr/bin/env python3
"""
seed_ha.py
==========
One-shot headless setup for a freshly-started Home Assistant container in
the dev/e2e harness:

  1. Waits for HA to come up.
  2. Drives HA's onboarding REST API to create the initial admin user
     (skips silently if onboarding is already done — e.g. a re-run against
     a persisted volume).
  3. Exchanges the onboarding auth code for a long-lived-ish access token.
  4. Drives the MQTT integration's config-flow API to add a config entry
     pointing at the mosquitto broker in this compose network. Modern Home
     Assistant removed YAML-configured MQTT brokers, so this scripted
     config-flow walk is the legitimate, current way to do this headlessly
     (the alternative — hand-writing a core.config_entries dict into
     .storage — is version-fragile and undocumented; this uses the same
     public REST API the frontend itself calls).
  5. Writes the resulting long-lived access token to /seed-out/token.txt so
     the Playwright test can log in without walking the login UI, and the
     username/password to /seed-out/credentials.json.

Steps that are already done are skipped, so a re-run against a still-running
HA (onboarding fully complete, container never restarted) is a no-op. Full
re-runs against a *persisted-but-restarted* ha-config volume are not
supported: the "user" onboarding step can only run once per volume, and
this harness's default `homeassistant` auth provider does not accept the
OAuth2 password grant used here as a fallback login (HA returns 400 for
grant_type=password unless the legacy legacy_api_password provider is
explicitly configured, which this harness does not do). For a clean re-run,
tear the stack down with `docker compose down -v` (recreates ha-config from
scratch) rather than reusing an existing volume — see README.md.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

HA_URL = os.environ.get("HA_URL", "http://homeassistant:8123")
USERNAME = os.environ.get("HA_SEED_USERNAME", "admin")
PASSWORD = os.environ.get("HA_SEED_PASSWORD", "adminpass123")
OUT_DIR = os.environ.get("SEED_OUT_DIR", "/seed-out")
MQTT_BROKER_HOST = os.environ.get("MQTT_BROKER_HOST", "mosquitto")
MQTT_BROKER_PORT = int(os.environ.get("MQTT_BROKER_PORT", "1883"))

CLIENT_ID = HA_URL + "/"


def _req(method: str, path: str, token: str | None = None, body: dict | None = None) -> tuple[int, dict]:
    url = f"{HA_URL}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw.decode(errors="replace")}


def wait_for_ha(timeout: int = 180) -> None:
    print("seed_ha: waiting for Home Assistant to come up...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _ = _req("GET", "/api/onboarding")
            if status in (200, 401):
                print("seed_ha: HA is up")
                return
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
            pass
        time.sleep(2)
    raise RuntimeError("Home Assistant did not come up in time")


def onboard() -> str:
    """Run onboarding if needed. Returns a bearer access token either way."""
    status, steps = _req("GET", "/api/onboarding")
    if status != 200:
        raise RuntimeError(f"GET /api/onboarding failed: {status} {steps}")

    done = {s["step"] for s in steps if s.get("done")}
    print(f"seed_ha: onboarding steps already done: {done}")

    if "user" not in done:
        status, resp = _req(
            "POST",
            "/api/onboarding/users",
            body={
                "client_id": CLIENT_ID,
                "name": "E2E Admin",
                "username": USERNAME,
                "password": PASSWORD,
                "language": "en",
            },
        )
        if status != 200:
            raise RuntimeError(f"onboarding/users failed: {status} {resp}")
        auth_code = resp["auth_code"]
        token = _exchange_code(auth_code)
    else:
        # Already onboarded (e.g. reused volume) — log in normally instead.
        token = _password_login()

    status, steps = _req("GET", "/api/onboarding", token=token)
    done = {s["step"] for s in steps if s.get("done")}

    if "core_config" not in done:
        status, resp = _req("POST", "/api/onboarding/core_config", token=token)
        print(f"seed_ha: core_config step -> {status}")

    if "analytics" not in done:
        status, resp = _req("POST", "/api/onboarding/analytics", token=token)
        print(f"seed_ha: analytics step -> {status}")

    if "integration" not in done:
        status, resp = _req(
            "POST",
            "/api/onboarding/integration",
            token=token,
            body={"client_id": CLIENT_ID, "redirect_uri": f"{HA_URL}/?auth_callback=1"},
        )
        print(f"seed_ha: integration step -> {status}")

    return token


def _exchange_code(auth_code: str) -> str:
    url = f"{HA_URL}/auth/token"
    form = (
        f"grant_type=authorization_code&code={auth_code}&client_id={urllib.parse.quote(CLIENT_ID)}"
    )
    req = urllib.request.Request(
        url,
        data=form.encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read())
    return payload["access_token"]


def _password_login() -> str:
    url = f"{HA_URL}/auth/token"
    form = (
        f"grant_type=password&username={urllib.parse.quote(USERNAME)}"
        f"&password={urllib.parse.quote(PASSWORD)}&client_id={urllib.parse.quote(CLIENT_ID)}"
    )
    req = urllib.request.Request(
        url,
        data=form.encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read())
    return payload["access_token"]


def setup_mqtt(token: str) -> None:
    status, resp = _req(
        "GET", "/api/config/config_entries/entry", token=token
    )
    if status == 200 and any(e.get("domain") == "mqtt" for e in resp):
        print("seed_ha: mqtt config entry already present, skipping")
        return

    status, flow = _req(
        "POST",
        "/api/config/config_entries/flow",
        token=token,
        body={"handler": "mqtt", "show_advanced_options": False},
    )
    if status != 200:
        raise RuntimeError(f"mqtt config flow init failed: {status} {flow}")

    flow_id = flow["flow_id"]
    # First step of the mqtt config flow (broker) — advanced options off.
    status, result = _req(
        "POST",
        f"/api/config/config_entries/flow/{flow_id}",
        token=token,
        body={
            "broker": MQTT_BROKER_HOST,
            "port": MQTT_BROKER_PORT,
            "username": "",
            "password": "",
        },
    )
    print(f"seed_ha: mqtt config flow step -> {status} {result.get('type')}")
    if status != 200 or result.get("type") not in ("create_entry", "abort"):
        raise RuntimeError(f"mqtt config flow did not complete: {status} {result}")


def main() -> None:
    wait_for_ha()
    token = onboard()
    setup_mqtt(token)

    os.makedirs(OUT_DIR, exist_ok=True)
    # Plaintext by design, not an oversight: USERNAME/PASSWORD are the
    # synthetic, hardcoded dev-harness admin credentials from
    # docker-compose.yml (HA_SEED_USERNAME/HA_SEED_PASSWORD), not real
    # secrets -- this file exists specifically so the Playwright test
    # (running as a separate host process, same Docker Compose network)
    # can read them back. Never used outside this throwaway local harness.
    with open(os.path.join(OUT_DIR, "credentials.json"), "w") as f:  # lgtm[py/clear-text-storage-sensitive-data]
        json.dump({"username": USERNAME, "password": PASSWORD, "ha_url": HA_URL}, f)
    with open(os.path.join(OUT_DIR, "token.txt"), "w") as f:
        f.write(token)

    print("seed_ha: done")


if __name__ == "__main__":
    main()
