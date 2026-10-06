"""Minimal stand-in for the Home Assistant Supervisor, for the dev/e2e harness.

The bridge never talks to Home Assistant directly. As an add-on it reaches
Core through the Supervisor, at fixed addresses:

  ws://supervisor/core/websocket   — registry watcher, Lovelace provisioning
  http://supervisor/core/api/...   — notifications, base URL / language lookup

authenticating with the SUPERVISOR_TOKEN the Supervisor injects into the
add-on's environment. The real Supervisor checks that token itself and talks
to Core with its own credentials, so the add-on's token never reaches Core.

This service does the same against the harness's real Home Assistant
container, under the compose service name ``supervisor`` so the bridge's
hardcoded hostnames resolve to it unchanged:

  - ``/core/websocket``: answers the bridge's auth handshake itself (accepting
    only FAKE_SUPERVISOR_TOKEN), opens its own authenticated WebSocket to
    Core, then relays every message both ways unmodified — so registry
    events reach the bridge exactly as Core emits them.
  - ``/core/api/<path>``: checks the bridge's bearer token, then forwards the
    request to Core's ``/api/<path>`` with its own token.
  - anything else (e.g. ``/services/mqtt``): 404, so run.sh falls back to its
    configured broker.

Core credentials: a long-lived access token this service creates itself on
startup, using the short-lived token ha-seed writes to /seed-out/token.txt.
Until that exists, requests get 503 and the bridge's own retry logic applies.

No production code is involved or changed. Dev harness only.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import aiohttp
from aiohttp import web

HA_URL = os.environ.get("HA_URL", "http://homeassistant:8123")
HA_WS_URL = HA_URL.replace("http://", "ws://", 1) + "/api/websocket"
FAKE_TOKEN = os.environ.get("FAKE_SUPERVISOR_TOKEN", "e2e-fake-supervisor-token")
SEED_TOKEN_FILE = os.environ.get("SEED_TOKEN_FILE", "/seed-out/token.txt")
# Delay before forwarding a config/entity_registry/list request to Core.
# Against this harness's local Core the full-registry fetch takes ~100ms;
# through a real Supervisor on real hardware it was observed taking well over
# a second. Timing races between the bridge's dashboard regen and its registry
# refresh only reproduce reliably at realistic latency, so specs get it here.
REGISTRY_LIST_DELAY_S = float(os.environ.get("REGISTRY_LIST_DELAY_S", "0"))

log = logging.getLogger("fake_supervisor")

# The long-lived Core token, once created. Read by request handlers.
_core_token: str | None = None


async def _create_long_lived_token(session: aiohttp.ClientSession, access_token: str) -> str:
    async with session.ws_connect(HA_WS_URL) as ws:
        await ws.receive_json()  # auth_required
        await ws.send_json({"type": "auth", "access_token": access_token})
        auth = await ws.receive_json()
        if auth.get("type") != "auth_ok":
            raise RuntimeError(f"seed token rejected by Core: {auth}")
        await ws.send_json(
            {
                "id": 1,
                "type": "auth/long_lived_access_token",
                # Core refuses a second token under an existing client_name,
                # and the stack can outlive this container (a rebuild starts a
                # new one against the same Core), so make it unique per start.
                "client_name": f"e2e fake supervisor {int(time.time())}",
                "lifespan": 3650,
            }
        )
        resp = await ws.receive_json()
        if not resp.get("success"):
            raise RuntimeError(f"could not create long-lived token: {resp}")
        return resp["result"]


def _read_seed_token() -> str:
    with open(SEED_TOKEN_FILE, encoding="utf-8") as f:
        return f.read()


async def _acquire_core_token(app: web.Application) -> None:
    """Keep trying until a long-lived Core token exists. The seed token file
    may be missing (seeding not done yet) or stale (left over from an
    earlier run, already expired), so both just mean "retry shortly"."""
    global _core_token
    async with aiohttp.ClientSession() as session:
        while _core_token is None:
            try:
                seed_token = (await asyncio.to_thread(_read_seed_token)).strip()
                if seed_token:
                    _core_token = await _create_long_lived_token(session, seed_token)
                    log.info("long-lived Core token created — proxy ready")
                    return
            except (OSError, RuntimeError, aiohttp.ClientError, ValueError) as e:
                log.info("Core token not available yet (%s) — retrying", e)
            await asyncio.sleep(3)


def _bridge_token_ok(request: web.Request) -> bool:
    return request.headers.get("Authorization", "") == f"Bearer {FAKE_TOKEN}"


async def handle_websocket(request: web.Request) -> web.StreamResponse:
    client = web.WebSocketResponse(heartbeat=None)
    await client.prepare(request)

    await client.send_json({"type": "auth_required", "ha_version": "fake-supervisor"})
    auth = await client.receive_json()
    if auth.get("type") != "auth" or auth.get("access_token") != FAKE_TOKEN:
        await client.send_json({"type": "auth_invalid", "message": "bad supervisor token"})
        await client.close()
        return client
    if _core_token is None:
        await client.send_json({"type": "auth_invalid", "message": "Core not ready yet"})
        await client.close()
        return client

    async with aiohttp.ClientSession() as session:
        try:
            core = await session.ws_connect(HA_WS_URL, max_msg_size=0)
        except aiohttp.ClientError as e:
            await client.send_json({"type": "auth_invalid", "message": f"Core unreachable: {e}"})
            await client.close()
            return client
        async with core:
            await core.receive_json()  # auth_required
            await core.send_json({"type": "auth", "access_token": _core_token})
            core_auth = await core.receive_json()
            await client.send_json(core_auth)
            if core_auth.get("type") != "auth_ok":
                await client.close()
                return client

            async def pump(src, dst, delay_registry_list: bool = False) -> None:
                async for msg in src:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        if (
                            delay_registry_list
                            and REGISTRY_LIST_DELAY_S
                            and '"config/entity_registry/list"' in msg.data
                        ):
                            await asyncio.sleep(REGISTRY_LIST_DELAY_S)
                        await dst.send_str(msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        await dst.send_bytes(msg.data)
                    else:
                        break

            tasks = [
                asyncio.create_task(pump(client, core, delay_registry_list=True)),
                asyncio.create_task(pump(core, client)),
            ]
            # Either side closing ends the relay, like the real proxy.
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in tasks:
                t.cancel()
    await client.close()
    return client


async def handle_core_api(request: web.Request) -> web.StreamResponse:
    if not _bridge_token_ok(request):
        return web.json_response({"message": "bad supervisor token"}, status=401)
    if _core_token is None:
        return web.json_response({"message": "Core not ready yet"}, status=503)
    url = f"{HA_URL}/api/{request.match_info['tail']}"
    headers = {"Authorization": f"Bearer {_core_token}"}
    if "Content-Type" in request.headers:
        headers["Content-Type"] = request.headers["Content-Type"]
    body = await request.read()
    async with (
        aiohttp.ClientSession() as session,
        session.request(
            request.method, url, headers=headers, data=body or None, params=request.query
        ) as resp,
    ):
        payload = await resp.read()
        return web.Response(
            status=resp.status,
            body=payload,
            content_type=resp.content_type,
        )


async def handle_other(request: web.Request) -> web.StreamResponse:
    return web.json_response(
        {"result": "error", "message": "not provided by fake supervisor"}, status=404
    )


async def _on_startup(app: web.Application) -> None:
    app["token_task"] = asyncio.create_task(_acquire_core_token(app))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    app = web.Application()
    app.router.add_get("/core/websocket", handle_websocket)
    app.router.add_route("*", "/core/api/{tail:.*}", handle_core_api)
    app.router.add_route("*", "/{tail:.*}", handle_other)
    app.on_startup.append(_on_startup)
    web.run_app(app, host="0.0.0.0", port=80, print=None)  # nosec B104 — compose-internal only


if __name__ == "__main__":
    main()
