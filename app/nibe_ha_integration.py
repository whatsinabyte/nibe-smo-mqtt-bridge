"""
nibe_ha_integration.py
======================
Home Assistant integration layer — everything that talks to HA itself rather
than to the Nibe device or the MQTT broker.

Responsibilities
----------------
- notify_ha / dismiss_ha: create/clear HA persistent notifications via the
  Supervisor REST API.
- HAEntityRegistryWatcher: a long-lived WebSocket subscriber that handles
  entity_registry_updated events in real time, replacing the previously
  required companion HA automation.
- create_management_handlers: subscribe to the management MQTT topics that
  the frontend card and HA buttons publish to (aid/smart mode, alarm
  reset, force poll, enable/disable entity, changelog read).

What this module does NOT do
-----------------------------
- No direct calls to the Nibe API.
- No MQTT discovery config publishing.
- No entity lifecycle management (those go through EntityManager).

Public surface
--------------
notify_ha(mqtt_client, title, message, notification_id)
dismiss_ha(mqtt_client, notification_id)
HAEntityRegistryWatcher(entity_manager, publisher)
    .start()
    .stop()
create_management_handlers(mqtt_client, entity_manager, publisher, mgmt_executor)
"""

import concurrent.futures
import json
import logging
import os
import re
import threading
import time
import urllib.request
from typing import Any
from nibe_utils import fmt_ts as _fmt_ts
from nibe_mqtt_publisher import (
    BrowserTopic,
    MgmtTopic,
    MqttDiscoveryPublisher,
)

log_mqtt     = logging.getLogger("nibe.mqtt")
log_commands = logging.getLogger("nibe.commands")
log_startup  = logging.getLogger("nibe.startup")
log_stats    = logging.getLogger("nibe.stats")
log_registry = logging.getLogger("nibe.registry")
log_history  = logging.getLogger("nibe.history")



# ============================================================================
# PERSISTENT NOTIFICATION HELPERS
# ============================================================================

_ha_base_url: str | None = None  # cached after first successful fetch


def _get_ha_base_url() -> str:
    """Return the HA instance base URL for use in notification links.

    Fetches ``internal_url`` (preferred) or ``external_url`` from the HA
    config API via the Supervisor proxy.  Falls back to the empty string so
    callers can always do ``f"{_get_ha_base_url()}/local/..."`` — the link
    will be a relative ``/local/...`` path when the host is unknown, which
    still works when copied into a browser, and avoids a broken absolute URL.

    Result is cached for the lifetime of the add-on process.
    """
    global _ha_base_url
    if _ha_base_url is not None:
        return _ha_base_url

    supervisor_token = os.environ.get('SUPERVISOR_TOKEN')
    if not supervisor_token:
        _ha_base_url = ''
        return _ha_base_url

    req = urllib.request.Request(
        "http://supervisor/core/api/config",
        headers={"Authorization": f"Bearer {supervisor_token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            cfg = json.loads(resp.read().decode())
        # Prefer internal_url; fall back to external_url; default to empty.
        url = cfg.get('internal_url') or cfg.get('external_url') or ''
        _ha_base_url = url.rstrip('/')
        log_mqtt.debug("HA base URL resolved: %r", _ha_base_url)
    except Exception as e:
        log_mqtt.warning("Could not fetch HA base URL: %s", e)
        _ha_base_url = ''

    return _ha_base_url


def notify_ha(mqtt_client, title: str, message: str, notification_id: str) -> None:
    """Create or replace a persistent notification in Home Assistant.

    Uses the HA Supervisor REST API.  Falls back to a log warning when running
    outside the HA add-on environment (no SUPERVISOR_TOKEN).

    ``mqtt_client`` is accepted for API compatibility but not used.
    """
    supervisor_token = os.environ.get('SUPERVISOR_TOKEN')
    if not supervisor_token:
        log_mqtt.warning(
            "HA notification (no supervisor token): [%s] %s", notification_id, title
        )
        return

    payload = json.dumps({
        "title":           title,
        "message":         message,
        "notification_id": notification_id,
    }).encode()

    req = urllib.request.Request(
        "http://supervisor/core/api/services/persistent_notification/create",
        data=payload,
        headers={
            "Authorization": f"Bearer {supervisor_token}",
            "Content-Type":  "application/json",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        log_mqtt.warning("HA notification sent: [%s] %s", notification_id, title)
    except Exception as e:
        log_mqtt.error("Failed to send HA notification: %s", e)


def dismiss_ha(mqtt_client, notification_id: str) -> None:
    """Dismiss a persistent notification in Home Assistant.

    ``mqtt_client`` is accepted for API compatibility but not used.
    """
    supervisor_token = os.environ.get('SUPERVISOR_TOKEN')
    if not supervisor_token:
        log_mqtt.info("HA notification dismiss (no supervisor token): [%s]", notification_id)
        return

    payload = json.dumps({"notification_id": notification_id}).encode()
    req = urllib.request.Request(
        "http://supervisor/core/api/services/persistent_notification/dismiss",
        data=payload,
        headers={
            "Authorization": f"Bearer {supervisor_token}",
            "Content-Type":  "application/json",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        log_mqtt.debug("HA notification dismissed: [%s]", notification_id)
    except Exception as e:
        log_mqtt.error("Failed to dismiss HA notification: %s", e)


# ============================================================================
# HA ENTITY REGISTRY WATCHER
# ============================================================================

class HAEntityRegistryWatcher:
    """Long-lived WebSocket subscriber for HA entity_registry_updated events.

    Handles HA-side entity enable/disable events in real time so the bridge
    stays in sync with the HA entity registry without requiring any user-installed
    automation.

    Parameters
    ----------
    entity_manager : EntityManager
        The bridge's entity lifecycle manager.  The watcher calls
        ``enable_entity``, ``disable_entity``, and ``build_disable_notification``
        on it — the minimal interface needed.
    publisher : MqttDiscoveryPublisher
        Used to republish discovery configs when a disable must be blocked
        (dynamic point or one with live dependents).
    """

    _INITIAL_BACKOFF = 2
    _MAX_BACKOFF     = 300  # cap at 5 minutes between reconnect attempts

    def __init__(self, entity_manager, publisher: MqttDiscoveryPublisher) -> None:
        self._em         = entity_manager
        self._pub        = publisher
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws_lock    = threading.Lock()
        self._current_ws = None
        self._msg_id     = 0
        self._unique_id_map: dict = {}
        # Coalesces refresh_registry() calls triggered by entity_registry_updated
        # events — see _schedule_refresh_registry() for why this exists.
        self._refresh_timer: threading.Timer | None = None
        self._refresh_timer_lock = threading.Lock()

    def start(self) -> None:
        """Start the background watcher thread."""
        supervisor_token = os.environ.get('SUPERVISOR_TOKEN')
        if not supervisor_token:
            log_registry.debug(
                "No SUPERVISOR_TOKEN — entity registry watcher disabled "
                "(running outside HA add-on environment)"
            )
            return
        self._thread = threading.Thread(
            target=self._run, name="nibe_registry_watcher", daemon=True
        )
        self._thread.start()
        log_registry.info("Entity registry watcher started")

    def entity_id_for(self, point_id: int) -> str | None:
        """Return the HA entity_id for a Nibe point, or None if not registered.

        Uses the local cache populated from the initial registry fetch and
        live registry events. Returns None if not found.
        """
        return self._unique_id_map.get(f"nibe_{point_id}")

    def refresh_registry(self) -> None:
        """Re-fetch the full entity registry and refresh the local cache.

        Called when entity_id_for returns None for a point that is known to
        be active — handles the case where HA registered the entity but the
        registry event was missed or had unexpected structure.
        """
        token = os.environ.get('SUPERVISOR_TOKEN', '')
        if not token:
            return
        try:
            import websocket as _ws_lib
            ws = _ws_lib.create_connection(
                "ws://supervisor/core/websocket",
                timeout=10,
            )
            # Authenticate — recv auth_required first, then send auth, then check result.
            greeting = json.loads(ws.recv())
            if greeting.get("type") != "auth_required":
                log_registry.debug("Registry refresh: unexpected greeting %s", greeting.get("type"))
                ws.close()
                return
            ws.send(json.dumps({"type": "auth", "access_token": token}))
            auth_result = json.loads(ws.recv())
            if auth_result.get("type") != "auth_ok":
                log_registry.warning("Registry refresh: auth failed (%s)", auth_result.get("type"))
                ws.close()
                return
            # Fetch registry
            ws.send(json.dumps({"id": 1, "type": "config/entity_registry/list"}))
            raw = ws.recv()
            resp = json.loads(raw)
            ws.close()
            if resp.get("success"):
                count = 0
                for entry in resp.get("result", []):
                    uid = entry.get("unique_id")
                    eid = entry.get("entity_id")
                    if uid and eid and uid.startswith("nibe_"):
                        self._unique_id_map[uid] = eid
                        count += 1
                log_registry.debug("Registry refresh: updated %d nibe entries", count)
        except Exception as e:
            log_registry.debug("Registry refresh failed: %s", e)

    _REFRESH_DEBOUNCE_S = 5.0

    def _schedule_refresh_registry(self) -> None:
        """Coalesce refresh_registry() calls that arrive in a burst into a
        single call after the burst settles, rather than one full
        WebSocket round-trip per entity.

        refresh_registry() opens a brand-new WebSocket connection to the
        Supervisor, does a full auth handshake, and fetches the entire
        entity registry — every call is expensive. Without coalescing,
        every entity_registry_updated "create" event that lacks a
        unique_id (which per HA's own MQTT-entity behaviour is normal for
        essentially every newly created entity) independently scheduled
        its own refresh_registry() call. Enabling a large point set in one
        go — e.g. a mode change or a fresh install into a large mode —
        creates that many entities in a tight window, so that many nearly
        simultaneous WebSocket connections were opened to the Supervisor
        at once. In production this was observed to overwhelm the
        Supervisor's WebSocket proxy: most calls timed out, and once
        enough piled up the connection started failing outright with
        broken-pipe errors.

        Cancel-and-reschedule debounce: each call cancels any pending
        timer and starts a fresh one, so a burst of N events — however
        large — results in exactly one refresh_registry() call, fired
        _REFRESH_DEBOUNCE_S after the last event in the burst.
        """
        with self._refresh_timer_lock:
            if self._refresh_timer is not None:
                self._refresh_timer.cancel()
            t = threading.Timer(self._REFRESH_DEBOUNCE_S, self.refresh_registry)
            t.daemon = True
            t.name = "nibe_registry_refresh_debounce"
            log_registry.debug("Scheduling registry refresh (debounce)")
            self._refresh_timer = t
            t.start()

    def stop(self) -> None:
        """Signal the watcher thread to exit and wait briefly for it to finish."""
        self._stop_event.set()
        with self._refresh_timer_lock:
            if self._refresh_timer is not None:
                self._refresh_timer.cancel()
                self._refresh_timer = None
        with self._ws_lock:
            if self._current_ws:
                try:
                    self._current_ws.close()
                except Exception:
                    pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        log_registry.debug("Entity registry watcher stopped")

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    def _connect_and_subscribe(self, token: str) -> object:
        import websocket
        ws = websocket.create_connection("ws://supervisor/core/websocket", timeout=10)

        greeting = json.loads(ws.recv())
        if greeting.get("type") != "auth_required":
            ws.close()
            raise RuntimeError(
                f"Unexpected WS greeting type: {greeting.get('type', 'unknown')}"
            )

        ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth_result = json.loads(ws.recv())
        if auth_result.get("type") != "auth_ok":
            ws.close()
            raise RuntimeError(
                f"WS auth failed (response type: {auth_result.get('type', 'unknown')})"
            )

        sub_id = self._next_id()
        ws.send(json.dumps({
            "id": sub_id, "type": "subscribe_events",
            "event_type": "entity_registry_updated",
        }))
        sub_result = json.loads(ws.recv())
        if not sub_result.get("success"):
            ws.close()
            raise RuntimeError(f"Event subscription failed: {sub_result}")

        self._unique_id_map = self._fetch_entity_registry(ws)

        # Set a per-recv timeout equal to the ping interval so the event
        # loop wakes up regularly to send keepalive pings. Without pings,
        # a stale connection goes undetected for up to the full recv timeout.
        # _PING_INTERVAL_S drives the recv timeout; _PING_TIMEOUT_S is how
        # long to wait for a pong before treating the connection as dead.
        ws.settimeout(self._PING_INTERVAL_S)
        log_registry.debug(
            "WebSocket connected and subscribed to entity_registry_updated events"
        )
        return ws

    _MAX_CONSEC_FAILURES = 10
    _PING_INTERVAL_S     = 30    # send a ping after this many seconds of silence
    _PING_TIMEOUT_S      = 15    # reconnect if no pong arrives within this long

    def _run(self) -> None:
        """Main loop: connect → recv events → reconnect on failure.

        Gives up after _MAX_CONSEC_FAILURES consecutive connection failures
        to avoid looping forever when the supervisor WebSocket is permanently
        unavailable. The counter resets to zero on any successful connection.
        """
        token           = os.environ.get('SUPERVISOR_TOKEN', '')
        backoff         = self._INITIAL_BACKOFF
        consec_failures = 0

        while not self._stop_event.is_set():
            ws: Any = None
            try:
                ws = self._connect_and_subscribe(token)
                with self._ws_lock:
                    self._current_ws = ws
                backoff         = self._INITIAL_BACKOFF
                consec_failures = 0

                # Import the websocket timeout exception for keepalive
                # detection.  The import is safe here — if websocket-client
                # weren't installed we would have failed in _connect_and_subscribe.
                _WsTimeout: type[BaseException]
                try:
                    from websocket import WebSocketTimeoutException as _WsTimeout
                except ImportError:
                    _WsTimeout = TimeoutError

                ping_sent_at: float = 0.0

                while not self._stop_event.is_set():
                    try:
                        raw = ws.recv()
                    except _WsTimeout:
                        # recv timed out after _PING_INTERVAL_S — send ping
                        now = time.time()
                        if ping_sent_at > 0 and now - ping_sent_at > self._PING_TIMEOUT_S:
                            raise ConnectionError(
                                f"WebSocket keepalive timeout — no pong received "
                                f"in {self._PING_TIMEOUT_S}s after ping"
                            )
                        ws.send(json.dumps({
                            "id": self._next_id(), "type": "ping",
                        }))
                        ping_sent_at = now
                        continue

                    # Any received frame (including pong) resets ping state
                    ping_sent_at = 0.0

                    if not raw:
                        break
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue

                    if msg.get("type") == "pong":
                        continue
                    if msg.get("type") == "event":
                        try:
                            self._handle_event(msg.get("event", {}))
                        except Exception as e:
                            log_registry.warning(
                                "Error handling registry event: %s", e, exc_info=True
                            )

            except ImportError:
                log_registry.warning(
                    "websocket-client not installed — entity registry watcher cannot run. "
                    "Add 'websocket-client' to requirements.txt."
                )
                return

            except Exception as e:
                if self._stop_event.is_set():
                    break
                consec_failures += 1
                if consec_failures >= self._MAX_CONSEC_FAILURES:
                    log_registry.warning(
                        "Registry watcher: %d consecutive failures — giving up. "
                        "HA-side entity enable/disable events will not be detected.",
                        consec_failures,
                    )
                    return
                log_registry.warning(
                    "Registry watcher disconnected (%s) — reconnecting in %ds "
                    "(failure %d/%d)",
                    e, backoff, consec_failures, self._MAX_CONSEC_FAILURES,
                )
                self._stop_event.wait(timeout=backoff)
                backoff = min(backoff * 2, self._MAX_BACKOFF)

            finally:
                with self._ws_lock:
                    self._current_ws = None
                if ws:
                    try:
                        ws.close()
                    except Exception:
                        pass

        log_registry.debug("Registry watcher thread exiting")

    def _fetch_entity_registry(self, ws) -> dict:
        """Fetch unique_id → entity_id mapping from the HA entity registry.

        Loops recv() until the response matching req_id arrives, discarding
        any interleaved entity_registry_updated events that HA may push
        between the request and the list response.
        """
        req_id = self._next_id()
        ws.send(json.dumps({"id": req_id, "type": "config/entity_registry/list"}))
        resp = None
        try:
            ws.settimeout(30)
            while True:
                raw = ws.recv()
                msg = json.loads(raw)
                if msg.get("id") == req_id:
                    resp = msg
                    break
                # Discard interleaved push messages (e.g. entity_registry_updated
                # events arriving while our list request is in flight).
                log_registry.debug(
                    "Registry fetch: discarding interleaved message type=%s id=%s",
                    msg.get("type"), msg.get("id"),
                )
        except Exception as e:
            log_registry.warning("Could not fetch entity registry (timeout or error): %s", e)
            return {}
        finally:
            ws.settimeout(None)
        if not resp or not resp.get("success"):
            log_registry.warning("Could not fetch entity registry: %s", resp)
            return {}
        mapping = {}
        result = resp.get("result", [])
        for entry in result:
            uid = (entry.get("unique_id")
                   or entry.get("config", {}).get("unique_id")
                   or entry.get("options", {}).get("unique_id"))
            eid = entry.get("entity_id")
            if uid and eid:
                mapping[uid] = eid
        nibe_count = sum(1 for k in mapping if k.startswith("nibe_"))
        log_registry.debug(
            "Entity registry cached: %d total entries, %d nibe entries",
            len(mapping), nibe_count,
        )
        return mapping

    def _handle_event(self, event: dict) -> None:
        """Process a single entity_registry_updated event payload."""
        data      = event.get("data", {})
        action    = data.get("action")
        entity_id = data.get("entity_id", "unknown")
        log_registry.debug("Registry event: action=%s, entity_id=%s", action, entity_id)

        if action == "create":
            eid = data.get("entity_id")
            uid = data.get("unique_id") or data.get("config", {}).get("unique_id")
            if uid and eid:
                self._unique_id_map[uid] = eid
            elif eid:
                # HA create event lacks unique_id (known HA behaviour for MQTT
                # entities) — schedule a debounced registry refresh to populate
                # the map. Debounced (not a fixed per-event delay) because a
                # large batch of entities created together — e.g. a mode
                # change — fires this branch once per entity; without
                # coalescing, each would independently open its own
                # WebSocket connection to the Supervisor. See
                # _schedule_refresh_registry() for the full story.
                self._schedule_refresh_registry()
            return

        if action == "update":
            eid = data.get("entity_id")
            uid = data.get("unique_id") or data.get("config", {}).get("unique_id")
            if uid and eid:
                self._unique_id_map[uid] = eid
            elif eid:
                self._schedule_refresh_registry()

            # Detect HA-side enable/disable via the disabled_by field change.
            # prev_disabled == "user" means the entity WAS disabled → now enabled.
            # prev_disabled == None  means the entity WAS enabled  → now disabled.
            changes = data.get("changes", {})
            if "disabled_by" in changes and eid:
                prev_disabled = changes["disabled_by"]
                if prev_disabled == "user":
                    self._on_entity_enabled(eid)
                elif prev_disabled is None:
                    self._on_entity_disabled(eid)
            return

        if action == "remove":
            # Clean up the local map so stale unique_id → entity_id entries
            # do not accumulate over time (Finding 7 — _unique_id_map growth).
            uid = data.get("unique_id") or data.get("config", {}).get("unique_id")
            if uid:
                self._unique_id_map.pop(uid, None)
            return

    def _on_entity_enabled(self, ha_entity_id: str) -> None:
        """Handle a HA-side entity re-enable."""
        point_id = self._em.resolve_point_from_entity_id(
            ha_entity_id, unique_id_map=self._unique_id_map
        )
        if point_id is None:
            return

        log_registry.debug(
            "Entity %s (point %s) re-enabled via HA — republishing discovery",
            ha_entity_id, point_id,
        )

        safe_id  = ha_entity_id.replace('.', '_').replace('-', '_')[:60]
        notif_id = f'nibe_ha_disable_{safe_id}'
        dismiss_ha(self._em.mqtt, notif_id)

        if point_id not in self._em.mqtt_enabled_points:
            self._em.enable_entity(point_id)
            _publish_stats(self._em, self._pub)
        else:
            point_dict = self._em.all_points_by_id.get(point_id)
            if point_dict:
                self._pub.publish_entity_discovery(point_dict, self._em.bulk_data)

        title, message, _ = self._em.build_disable_notification(
            point_id, ha_entity_id, action='re-enabled'
        )
        notify_ha(self._em.mqtt, title=title, message=message, notification_id=notif_id)

    def _on_entity_disabled(self, ha_entity_id: str) -> None:
        """Handle a HA-side entity disable."""
        point_id = self._em.resolve_point_from_entity_id(
            ha_entity_id, unique_id_map=self._unique_id_map
        )
        if point_id is None:
            return

        point      = self._em.all_points_by_id.get(point_id)
        is_dynamic = point.get('is_dynamic', False) if point else False

        # No controller map in the simplified design — live_dependents
        # is always empty. Dynamic points manage their own lifecycle via
        # the bulk fetch detection loop.
        live_dependents: list[str] = []

        log_registry.debug(
            "Entity %s (point %s) disabled via HA — mirroring disable",
            ha_entity_id, point_id,
        )

        title, message, notif_id = self._em.build_disable_notification(
            point_id, ha_entity_id, action='disabled'
        )

        if is_dynamic or live_dependents:
            point_dict = self._em.all_points_by_id.get(point_id)
            if point_dict:
                self._pub.publish_entity_discovery(point_dict, self._em.bulk_data)
            log_registry.info(
                "Republished discovery config for point %s to reverse HA-side disable", point_id
            )

        else:
            self._em.disable_entity(point_id)
            _publish_stats(self._em, self._pub)
            log_registry.info("Mirrored HA-side disable for point %s in bridge", point_id)
            return   # no confusing notification for an intentional disable

        notify_ha(self._em.mqtt, title=title, message=message, notification_id=notif_id)


# ============================================================================
# MANAGEMENT COMMAND HANDLERS
# ============================================================================


class ManagementCommandHandler:
    """Registers and handles all management MQTT topics.

    Instantiate and call ``register_all()`` once after management discovery
    configs have been published.  Each handler dispatches blocking work to
    ``mgmt_executor`` so the MQTT callback thread is never held.
    """

    def __init__(
        self,
        mqtt_client,
        entity_manager,
        publisher: MqttDiscoveryPublisher,
        mgmt_executor: concurrent.futures.ThreadPoolExecutor,
    ) -> None:
        self._mqtt     = mqtt_client
        self._em       = entity_manager
        self._pub      = publisher
        self._executor = mgmt_executor
        self._test_running = threading.Event()

    def register_all(self) -> None:
        """Subscribe to all management topics and wire up their handlers.

        Every subscription is also recorded with
        ``entity_manager.register_mgmt_subscription`` so that
        ``resubscribe_all()`` can replay it after a Mosquitto restart.
        """
        self._sub(MgmtTopic.SMART_SET,           self._handle_smart_mode)
        self._sub(MgmtTopic.AID_SET,             self._handle_aid_mode)
        self._sub(MgmtTopic.ALARM_RESET_PRESS,   self._handle_reset_alarms)
        self._sub(MgmtTopic.FORCE_POLL_PRESS,    self._handle_force_poll)
        self._sub(MgmtTopic.REGEN_DASH_PRESS,    self._handle_regen_dashboard)
        self._sub(MgmtTopic.ENABLE_SET,          self._handle_enable)
        self._sub(MgmtTopic.DISABLE_SET,         self._handle_disable)
        self._sub(MgmtTopic.CHANGELOG_READ_PRESS, self._handle_changelog_reset)
        self._sub(MgmtTopic.FLUSH_MAP_PRESS,     self._handle_flush_dynamic_map)
        self._sub(MgmtTopic.RUN_TESTS_PRESS,     self._handle_run_tests)
        self._em.mqtt.subscribe(BrowserTopic.SNAPSHOTS_CMD, qos=1)
        self._em.mqtt.message_callback_add(
            BrowserTopic.SNAPSHOTS_CMD, self._handle_snapshot_cmd
        )

    # ── Internal helper ───────────────────────────────────────────────────────

    def _sub(self, topic: str, handler, qos: int = 1) -> None:
        """Subscribe, add callback, and record for resubscription on reconnect."""
        self._mqtt.subscribe(topic, qos=qos)
        self._mqtt.message_callback_add(topic, handler)
        self._em.register_mgmt_subscription(topic, handler, qos)

    # ── Handlers ──────────────────────────────────────────────────────────────

    def _handle_smart_mode(self, _client, _userdata, message) -> None:
        value = message.payload.decode().strip().lower()
        if value not in ("normal", "away"):
            log_commands.warning("Invalid smart mode value: %s", value)
            return
        def _do():
            if self._em._api.write_device_mode("smartmode", value):
                self._mqtt.publish(MgmtTopic.SMART_STATE, value, retain=True)
                self._em.device_modes_dirty = True
        self._executor.submit(_do)

    def _handle_aid_mode(self, _client, _userdata, message) -> None:
        payload = message.payload.decode().strip()
        value   = "on" if payload in ("ON", "1", "on", "true", "True") else "off"
        def _do():
            if self._em._api.write_device_mode("aidmode", value):
                self._mqtt.publish(
                    MgmtTopic.AID_STATE,
                    "ON" if value == "on" else "OFF",
                    retain=True,
                )
                self._em.device_modes_dirty = True
        self._executor.submit(_do)

    def _handle_reset_alarms(self, _client, _userdata, _message) -> None:
        def _do():
            if self._em._api.reset_notifications():
                self._mqtt.publish(MgmtTopic.ALARM_STATE, "0", retain=True)
                self._mqtt.publish(
                    MgmtTopic.ALARM_ATTRS,
                    json.dumps({"alarms": [], "last_updated": _fmt_ts()}),
                    retain=True,
                )
        self._executor.submit(_do)

    def _handle_force_poll(self, _client, _userdata, _message) -> None:
        def _do():
            log_startup.info("Force poll triggered from HA")
            self._em.update_all_states(force=True)
            update_stats_and_health(self._em, self._pub)
            _publish_device_modes(self._em, self._pub)
        self._executor.submit(_do)

    def _handle_regen_dashboard(self, _client, _userdata, _message) -> None:
        log_startup.info("Regenerate Dashboard triggered from HA")
        cb = self._em._on_enabled_state_change
        if cb is not None:
            cb()
        else:
            log_startup.warning("Regenerate Dashboard: no callback registered")

    def _handle_enable(self, _client, _userdata, message) -> None:
        raw = message.payload.decode().strip()
        def _do():
            try:
                point_id = int(raw)
                if self._em.enable_entity(point_id):
                    _publish_stats(self._em, self._pub)
            except ValueError:
                log_commands.warning("handle_enable: invalid point id '%s'", raw)
        self._executor.submit(_do)

    def _handle_disable(self, _client, _userdata, message) -> None:
        raw = message.payload.decode().strip()
        def _do():
            try:
                point_id = int(raw)
                if self._em.disable_entity(point_id):
                    _publish_stats(self._em, self._pub)
            except ValueError:
                log_commands.warning("handle_disable: invalid point id '%s'", raw)
        self._executor.submit(_do)

    def _handle_changelog_reset(self, _client, _userdata, _message) -> None:
        log_history.info("Changelog reset requested by user")
        self._em.mark_changelog_read()

    def _handle_flush_dynamic_map(self, _client, _userdata, _message) -> None:
        log_commands.warning("Flush Dynamic Map triggered from HA (debug)")
        def _do():
            entity_types = {
                pid: pt.get("entity_type", "")
                for pid, pt in self._em.all_points_by_id.items()
            }
            self._em.dynamic_point_map.flush(self._em.all_points_by_id, entity_types)
            self._em._persist_dynamic_map()
            log_commands.warning("Dynamic map flushed — all entries reset to unprocessed")
        self._executor.submit(_do)

    def _handle_snapshot_cmd(self, _client, _userdata, message) -> None:
        """Handle snapshot commands from the card via nibe/browser/snapshots/cmd.

        Expected payload (JSON):
            {"action": "save",    "name": "Summer Profile"}
            {"action": "restore", "name": "Summer Profile", "mode": "flush|merge"}
            {"action": "delete",  "name": "Summer Profile"}
        """
        try:
            cmd = json.loads(message.payload.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            log_commands.warning("snapshot_cmd: invalid payload: %s", e)
            return

        action = cmd.get('action', '').strip().lower()
        name   = cmd.get('name', '').strip()

        def _do() -> None:
            if action == 'save':
                ok, msg = self._em.save_snapshot(name)
            elif action == 'restore':
                mode = cmd.get('mode', 'flush').strip().lower()
                if mode not in ('flush', 'merge'):
                    log_commands.warning(
                        "snapshot_cmd restore: unknown mode '%s', using flush", mode
                    )
                    mode = 'flush'
                ok, msg = self._em.restore_snapshot(name, mode)
                if ok:
                    _publish_stats(self._em, self._pub)
            elif action == 'delete':
                ok, msg = self._em.delete_snapshot(name)
            else:
                log_commands.warning("snapshot_cmd: unknown action '%s'", action)
                return
            log_commands.info("snapshot_cmd %s '%s': %s", action, name, msg)

        self._executor.submit(_do)

    def _handle_run_tests(self, _client, _userdata, _message) -> None:
        """Run the full pytest suite in a background thread.

        Publishes progress and final result to MgmtTopic.RUN_TESTS_STATE /
        RUN_TESTS_ATTRS, then sends a HA persistent notification with a
        copy-pasteable summary.  Runs with HYPOTHESIS_PROFILE=nightly so
        nightly runs exercise maximum Hypothesis coverage.
        """
        log_commands.warning("Run Test Suite triggered from HA (debug)")

        if self._test_running.is_set():
            log_commands.warning(
                "Test suite already running — ignoring duplicate trigger"
            )
            return
        self._test_running.set()

        def _do() -> None:
            try:
                import subprocess
                import os
                import json as _json
                import time as _time

                import sys as _sys
                addon_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                test_path = "/tests"
                if not os.path.isdir(test_path):
                    # Fallback for development layout (tests/ alongside app/)
                    test_path = os.path.join(addon_dir, "tests")

                # Determine working directory — pytest.ini lives at addon root
                # and configures testpaths/pythonpath relative to it.
                pytest_ini = os.path.join(addon_dir, "pytest.ini")
                run_dir = addon_dir if os.path.exists(pytest_ini) else "/tests"

                python_exe = _sys.executable or "python3"
                env = {**os.environ,
                       "HYPOTHESIS_PROFILE": "nightly",
                       "PYTHONPATH": os.path.join(addon_dir, "app")}

                # Publish 'running' state immediately
                self._em.mqtt.publish(MgmtTopic.RUN_TESTS_STATE, "running", retain=True)
                self._em.mqtt.publish(MgmtTopic.RUN_TESTS_ATTRS, _json.dumps({
                    "status": "running",
                    "started": _time.strftime("%Y-%m-%d %H:%M:%S"),
                }), retain=True)

                t_start = _time.monotonic()
                report_path = "/config/www/nibe_test_report.html"
                try:
                    proc = subprocess.run(
                        [python_exe, "-m", "pytest", test_path,
                         f"--html={report_path}",
                         "--tb=short",    # full traceback on failures
                         "--no-header",   # skip pytest version header
                         "-q",            # compact: N passed in Xs
                         "--timeout=600", # per-test cap; nightly stateful tests exceed pytest.ini default of 300s
                         "-n", "auto",    # xdist: one worker per CPU core (~4 on ODROID-M1)
                        ],
                        capture_output=True, text=True,
                        cwd=run_dir, env=env,
                        timeout=14400,  # 4 hour hard limit
                    )
                    elapsed   = _time.monotonic() - t_start
                    exit_code = proc.returncode
                    output    = (proc.stdout + proc.stderr).strip()
                except subprocess.TimeoutExpired:
                    elapsed   = _time.monotonic() - t_start
                    exit_code = -1
                    output    = ("Test suite process killed after 4-hour hard limit.\n"
                            "The nightly profile (500 examples, stateful_step_count=50) exceeded\n"
                            "the subprocess timeout. Consider reducing max_examples in conftest.py.")
                except Exception as exc:
                    elapsed   = _time.monotonic() - t_start
                    exit_code = -2
                    output    = f"Failed to run test suite: {exc}"

                # Post-process the HTML report: inject a mobile viewport meta tag
                # and relax the min-width so the report is readable on phones.
                try:
                    with open(report_path, "r", encoding="utf-8") as _f:
                        _html = _f.read()
                    _html = _html.replace(
                        '<meta charset="utf-8"/>',
                        '<meta charset="utf-8"/>\n'
                        '    <meta name="viewport" '
                        'content="width=device-width, initial-scale=1"/>',
                    )
                    _html = _html.replace("min-width: 800px", "min-width: 320px")

                    with open(report_path, "w", encoding="utf-8") as _f:
                        _f.write(_html)
                except FileNotFoundError:
                    log_commands.warning(
                        "Test suite HTML report not found at %s — "
                        "pytest-html may not be installed in the Docker image. "
                        "Check requirements-test.txt and rebuild the add-on.",
                        report_path,
                    )
                except Exception as _e:
                    log_commands.warning(
                        "Could not post-process HTML report at %s: %s",
                        report_path, _e,
                    )

                passed = exit_code == 0
                if passed:
                    status = "passed"
                elif exit_code == -1:
                    status = "timed_out"
                elif exit_code == -2:
                    status = "error"
                else:
                    status = "failed"

                # ── Extract the pytest counts line ────────────────────────────
                # Always the last non-empty line, e.g. "1 failed, 2251 passed in 1:10:22"
                lines = output.splitlines()
                counts_line = next(
                    (ln.strip() for ln in reversed(lines) if ln.strip()), ""
                )

                # ── Extract failure details for the notification ───────────────
                # Pull the "short test summary info" block — one line per failure:
                # "FAILED tests/test_x.py::Class::test - ErrorType: message"
                # Falls back to E-prefixed assertion lines from the FAILURES section.
                def _extract_failure_lines(text: str) -> list[str]:
                    result: list[str] = []
                    in_short = False
                    for ln in text.splitlines():
                        if "short test summary info" in ln:
                            in_short = True
                            continue
                        if in_short:
                            if ln.startswith("FAILED "):
                                result.append(ln[len("FAILED "):].strip())
                            elif ln.startswith("="):
                                break
                    if result:
                        return result
                    # Fallback: E-prefixed assertion lines from the FAILURES section
                    in_failures = False
                    block: list[str] = []
                    for ln in text.splitlines():
                        if re.search(r"={3,} FAILURES ={3,}", ln):
                            in_failures = True
                            continue
                        if in_failures:
                            if re.search(r"={3,}", ln):
                                break
                            block.append(ln)
                    e_lines = [ln2.lstrip() for ln2 in block if ln2.strip().startswith("E ")]
                    return e_lines[:5] if e_lines else block[:10]

                # ── Build the sensor summary (stored in attributes tab) ────────
                # Pass: strip progress-dot lines, keep warnings + counts line.
                # Fail: short summary block + counts line.
                # xdist/pytest infrastructure lines to suppress from the summary
                _NOISE_PREFIXES = (
                    "bringing up nodes",
                    "Generated html report",
                    "=== ",
                    "--- ",
                )

                if exit_code == 0:
                    meaningful = [
                        ln for ln in lines
                        if ln.strip()
                        and not set(ln.strip()).issubset(set(".FEx[] |\t0123456789%u"))
                        and not ln.strip().lower().startswith(_NOISE_PREFIXES)
                    ]
                    if counts_line and counts_line not in meaningful:
                        meaningful.append(counts_line)
                    summary = "\n".join(meaningful) if meaningful else counts_line
                else:
                    fail_lines = _extract_failure_lines(output)
                    parts = fail_lines + ([counts_line] if counts_line else [])
                    summary = "\n".join(parts) if parts else output[-2000:]

                timestamp = _time.strftime("%Y-%m-%d %H:%M:%S")

                # Format elapsed time readably
                if elapsed < 60:
                    elapsed_str = f"{elapsed:.1f}s"
                else:
                    elapsed_str = f"{int(elapsed // 60)}m {elapsed % 60:.0f}s"

                log_commands.warning(
                    "Test suite %s in %s (exit code %d)",
                    status, elapsed_str, exit_code,
                )

                # Publish result sensor
                self._em.mqtt.publish(MgmtTopic.RUN_TESTS_STATE, status, retain=True)
                self._em.mqtt.publish(MgmtTopic.RUN_TESTS_ATTRS, _json.dumps({
                    "status":    status,
                    "exit_code": exit_code,
                    "elapsed_s": round(elapsed, 1),
                    "elapsed":   elapsed_str,
                    "timestamp": timestamp,
                    "summary":   summary,
                }), retain=True)

                # HA persistent notification — only on failure.
                # On success the result is visible on the test suite sensor
                # (sensor attributes tab). On pass: dismiss any previous failure
                # notification. On fail: send a focused notification showing the
                # failing test name and assertion, with a clickable report link.
                if passed:
                    dismiss_ha(self._em.mqtt, "nibe_test_suite_result")
                else:
                    _MAX_NOTIF = 2048
                    timed_out    = exit_code == -1
                    launch_error = exit_code == -2
                    if timed_out:
                        title = "Nibe Test Suite — ⏱ TIMED OUT"
                        body  = (
                            "The test process was killed before it finished. "
                            "Reduce `max_examples` or `stateful_step_count` in "
                            "`tests/conftest.py` and rebuild the add-on."
                        )
                    elif launch_error:
                        title = "Nibe Test Suite — ⚠ LAUNCH ERROR"
                        body  = output
                    else:
                        title = "Nibe Test Suite — ❌ FAILED"
                        fail_lines = _extract_failure_lines(output)
                        if fail_lines:
                            # Format each as bold test path + assertion on next line
                            formatted: list[str] = []
                            for fl in fail_lines:
                                if " - " in fl:
                                    test_path, _, err_msg = fl.partition(" - ")
                                    formatted.append(
                                        f"**{test_path}**\n`{err_msg}`"
                                    )
                                else:
                                    formatted.append(f"**{fl}**")
                            body = "\n\n".join(formatted)
                        else:
                            body = f"```\n{summary}\n```"

                    message = (
                        f"{timestamp} — {counts_line} — {elapsed_str}\n\n"
                        f"{body}\n\n"
                        f"[View full report]({_get_ha_base_url()}/local/nibe_test_report.html)"
                    )
                    if len(message) > _MAX_NOTIF:
                        message = (
                            message[:_MAX_NOTIF - 60]
                            + "\n…\n\n"
                            f"[View full report]({_get_ha_base_url()}/local/nibe_test_report.html)"
                        )
                    notify_ha(
                        self._em.mqtt,
                        title=title,
                        message=message,
                        notification_id="nibe_test_suite_result",
                    )
            finally:
                self._test_running.clear()

        self._executor.submit(_do)


# ============================================================================
# POLL-LOOP HELPERS
# ============================================================================

def update_alarm_state(
    entity_manager,
    publisher: MqttDiscoveryPublisher,
) -> None:
    """Fetch /notifications and update the Active Alarms sensor + HA notification."""
    if entity_manager.api_consecutive_failures > 0:
        return

    alarms = entity_manager._api.fetch_notifications()
    if alarms is None:
        return

    alarm_count  = len(alarms)
    clean_alarms = [
        {
            "alarmId":     a.get("alarmId"),
            "header":      a.get("header", ""),
            "description": a.get("description", ""),
            "severity":    a.get("severity"),
            "time":        a.get("time", ""),
            "equipName":   a.get("equipName", ""),
        }
        for a in alarms
    ]

    publisher.publish_alarm_state(alarm_count, clean_alarms)
    # Log only when count changes — suppress steady-state zero noise
    if alarm_count != entity_manager._last_alarm_count:
        log_stats.debug("Alarm poll: %d active alarm(s)", alarm_count)
        entity_manager._last_alarm_count = alarm_count

    mqtt_client = entity_manager.mqtt

    if alarm_count > 0 and not entity_manager._alarm_notification_active:
        lines = []
        for a in clean_alarms:
            parts = [a.get("header", "Unknown alarm")]
            if a.get("equipName"):
                parts.append(f"Equipment: {a['equipName']}")
            if a.get("severity"):
                parts.append(f"Severity: {a['severity']}")
            desc = a.get("description", "")
            if desc and desc != a.get("header"):
                parts.append(desc)
            lines.append(" — ".join(parts))

        device_model = entity_manager.device_info.get('model', 'S-series')
        alarm_lines = "\n".join(f"• {line}" for line in lines)
        message = (
            f"{alarm_count} active alarm(s) on the Nibe {device_model}:\n"
            f"{alarm_lines}\n\n"
            f"Use the Reset Alarms button on the {device_model} Management device page "
            f"once the underlying issue is resolved."
        )
        notify_ha(
            mqtt_client,
            title=f"Nibe {device_model}: {alarm_count} Active Alarm(s)",
            message=message,
            notification_id="nibe_active_alarms",
        )
        entity_manager._alarm_notification_active = True

    elif alarm_count == 0 and entity_manager._alarm_notification_active:
        dismiss_ha(mqtt_client, "nibe_active_alarms")
        entity_manager._alarm_notification_active = False


def update_stats_and_health(entity_manager, publisher: MqttDiscoveryPublisher) -> None:
    """Publish all bridge health/stats sensors in one call."""
    _publish_stats(entity_manager, publisher)
    publisher.publish_uptime(
        entity_manager.bridge_start_time,
        entity_manager.api_last_success_time,
        entity_manager.api_consecutive_failures,
    )
    publisher.publish_api_reachability(
        entity_manager.api_consecutive_failures,
        entity_manager.api_failure_threshold,
        entity_manager.api_last_success_time,
        entity_manager.last_fetch_duration,
    )
    # Consolidated health snapshot — single retained topic with everything
    # an automation or external monitor needs to assess bridge health.
    with entity_manager._pending_writes_lock:
        pending = len(entity_manager.pending_writes)
    publisher.publish_bridge_status(
        bridge_start_time        = entity_manager.bridge_start_time,
        api_consecutive_failures = entity_manager.api_consecutive_failures,
        api_failure_threshold    = entity_manager.api_failure_threshold,
        api_last_success_time    = entity_manager.api_last_success_time,
        last_fetch_duration      = entity_manager.last_fetch_duration,
        write_total              = entity_manager._write_total,
        write_success            = entity_manager._write_success,
        write_failed             = entity_manager._write_failed,
        last_write_error         = entity_manager._last_write_error,
        pending_write_count      = pending,
        mqtt_enabled_count       = len(entity_manager.mqtt_enabled_points),
        all_points_count         = len(entity_manager.all_points_by_id),
        known_dynamic_count      = len(entity_manager.dynamic_point_map.all_known_dynamic_point_ids()),
    )


def update_device_modes(entity_manager, publisher: MqttDiscoveryPublisher) -> None:
    """Poll the device API for aid/smart mode and publish their states."""
    _publish_device_modes(entity_manager, publisher)


# ── Private helpers ────────────────────────────────────────────────────────────

def _publish_stats(entity_manager, publisher: MqttDiscoveryPublisher) -> None:
    with entity_manager._active_entities_lock:
        active_count = len(entity_manager.active_entities_by_id)

    publisher.publish_stats(
        all_points_count   = len(entity_manager.all_points_by_id),
        mqtt_enabled_count = len(entity_manager.mqtt_enabled_points),
        active_count       = active_count,
        type_counts        = dict(entity_manager._stats_type_counts),
        category_counts    = dict(entity_manager._stats_category_counts),
        writable_count     = entity_manager._stats_writable_count,
        write_total        = entity_manager._write_total,
        write_success      = entity_manager._write_success,
        write_failed       = entity_manager._write_failed,
    )
    mqtt_count  = len(entity_manager.mqtt_enabled_points)
    total_count = len(entity_manager.all_points_by_id)
    stats_key   = (mqtt_count, active_count, total_count)
    if getattr(entity_manager, '_last_stats_key', None) != stats_key:
        log_stats.debug(
            "Stats: MQTT=%d, Active=%d, Total=%d", mqtt_count, active_count, total_count,
        )
        entity_manager._last_stats_key = stats_key


def _publish_device_modes(entity_manager, publisher: MqttDiscoveryPublisher) -> None:
    """Publish aid mode and smart mode states.

    Uses a cache to avoid an extra fetch_device_info() API call on every
    poll cycle.  The cache is marked dirty on startup and after any write
    to either mode, so the next call always fetches fresh values when needed.
    """
    if entity_manager.api_consecutive_failures > 0:
        return

    if not entity_manager.device_modes_dirty and entity_manager.device_modes_cache:
        publisher.publish_device_modes(
            aid_mode   = entity_manager.device_modes_cache.get("aidMode",   "off"),
            smart_mode = entity_manager.device_modes_cache.get("smartMode", "normal"),
        )
        return

    response = entity_manager._api.fetch_device_info()
    if not response:
        log_commands.warning("Could not fetch device mode states")
        return

    entity_manager.device_modes_cache = {
        "aidMode":   response.get("aidMode",   "off"),
        "smartMode": response.get("smartMode", "normal"),
    }
    entity_manager.device_modes_dirty = False

    publisher.publish_device_modes(
        aid_mode   = entity_manager.device_modes_cache["aidMode"],
        smart_mode = entity_manager.device_modes_cache["smartMode"],
    )