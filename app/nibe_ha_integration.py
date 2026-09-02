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
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from nibe_connectivity_check import run_connectivity_check
from nibe_mqtt_publisher import (
    BrowserTopic,
    MgmtTopic,
    MqttDiscoveryPublisher,
)
from nibe_test_runner import run_test_suite

from nibe_utils import fmt_ts as _fmt_ts

if TYPE_CHECKING:
    from nibe_entity_manager import EntityManager

log_mqtt = logging.getLogger("nibe.mqtt")
log_commands = logging.getLogger("nibe.commands")
log_startup = logging.getLogger("nibe.startup")
log_stats = logging.getLogger("nibe.stats")
log_registry = logging.getLogger("nibe.registry")
log_history = logging.getLogger("nibe.history")


# ============================================================================
# PERSISTENT NOTIFICATION HELPERS
# ============================================================================

_ha_base_url: str | None = None  # cached after first successful fetch
_ha_base_url_retry_after: float = 0.0  # time.time(); a failed fetch is retryable after this
_HA_BASE_URL_RETRY_COOLDOWN = 30.0  # seconds


def _get_ha_base_url() -> str:
    """Return the HA instance base URL for use in notification links.

    Fetches ``internal_url`` (preferred) or ``external_url`` from the HA
    config API via the Supervisor proxy.  Falls back to the empty string so
    callers can always do ``f"{_get_ha_base_url()}/local/..."`` — the link
    will be a relative ``/local/...`` path when the host is unknown, which
    still works when copied into a browser, and avoids a broken absolute URL.

    A successful result is cached for the lifetime of the add-on process —
    the URL doesn't change without a restart. A *failed* fetch is not cached
    that way: it's retried after a short cooldown instead. This call runs at
    startup, exactly when a transient "Supervisor API not up yet" hiccup is
    most likely, and caching that failure forever used to permanently
    degrade every future notification link to a relative path for the rest
    of the process's life. Missing SUPERVISOR_TOKEN is the one failure that
    genuinely is permanent — the environment doesn't change without a
    restart either — so that case is still cached forever.
    """
    global _ha_base_url, _ha_base_url_retry_after
    if _ha_base_url is not None:
        return _ha_base_url

    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
    if not supervisor_token:
        _ha_base_url = ""
        return _ha_base_url

    now = time.time()
    if now < _ha_base_url_retry_after:
        return ""

    # method="GET" is unobservable here: urllib's Request.get_method()
    # already defaults to "GET" whenever no data= is passed (verified
    # empirically), and this request never passes data.
    # Header key casing is unobservable — urllib.request.Request normalizes
    # header keys via .capitalize(). Verified empirically.
    req = urllib.request.Request(
        "http://supervisor/core/api/config",
        headers={"Authorization": f"Bearer {supervisor_token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(
            req, timeout=5
        ) as resp:  # hardcoded http://supervisor/ URL, not runtime-controllable input  # nosec B310
            cfg = json.loads(resp.read().decode())
        # Prefer internal_url; fall back to external_url; default to empty.
        url = cfg.get("internal_url") or cfg.get("external_url") or ""
        # Widening this char set (e.g. adding a char that never appears in
        # a real HA base URL) is unobservable — verified empirically, still
        # real/tested for the actual '/' char via
        # test_only_trailing_slashes_stripped_not_internal_ones.
        _ha_base_url = url.rstrip("/")
        log_mqtt.debug("HA base URL resolved: %r", _ha_base_url)
        return _ha_base_url
    except (
        urllib.error.URLError,
        OSError,
        TimeoutError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        AttributeError,
    ) as e:
        # URLError/OSError/TimeoutError: request failed (Supervisor
        # unreachable, timed out). JSONDecodeError/UnicodeDecodeError: the
        # response body wasn't valid UTF-8 JSON. AttributeError: the parsed
        # body wasn't a dict (cfg.get() doesn't exist on e.g. a JSON array).
        log_mqtt.warning("Could not fetch HA base URL: %s", e)
        _ha_base_url_retry_after = now + _HA_BASE_URL_RETRY_COOLDOWN
        return ""


_ha_language: str | None = None  # cached after first successful fetch
_ha_language_retry_after: float = 0.0  # time.time(); a failed fetch is retryable after this


def _get_ha_language() -> str:
    """Return Home Assistant's configured language, for auto-detecting the
    Nibe REST API query language when the ``language`` option is left blank.

    Fetches the ``language`` field from HA's config API via the Supervisor
    proxy — the same endpoint ``_get_ha_base_url()`` reads ``internal_url``/
    ``external_url`` from. Kept as an independent request (not sharing that
    cached response) so a failure or retry cooldown in one does not couple
    to the other, and each stays simple to test in isolation.

    A successful result is cached for the lifetime of the add-on process.
    A *failed* fetch is retried after a short cooldown rather than cached
    forever, for the same reason documented on ``_get_ha_base_url()``.
    Falls back to the empty string, meaning "use the API's default
    (English)" — callers must never crash or block startup on this being
    unavailable.
    """
    global _ha_language, _ha_language_retry_after
    if _ha_language is not None:
        return _ha_language

    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
    if not supervisor_token:
        _ha_language = ""
        return _ha_language

    now = time.time()
    if now < _ha_language_retry_after:
        return ""

    # method="GET" is unobservable here: urllib's Request.get_method()
    # already defaults to "GET" whenever no data= is passed (verified
    # empirically), and this request never passes data. Header key casing
    # is also unobservable — urllib.request.Request normalizes header keys
    # via .capitalize(). Verified empirically.
    req = urllib.request.Request(
        "http://supervisor/core/api/config",
        headers={"Authorization": f"Bearer {supervisor_token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(
            req, timeout=5
        ) as resp:  # hardcoded http://supervisor/ URL, not runtime-controllable input  # nosec B310
            cfg = json.loads(resp.read().decode())
        _ha_language = cfg.get("language") or ""
        log_mqtt.debug("HA language resolved: %r", _ha_language)
        return _ha_language
    except (
        urllib.error.URLError,
        OSError,
        TimeoutError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        AttributeError,
    ) as e:
        log_mqtt.warning("Could not fetch HA language: %s", e)
        _ha_language_retry_after = now + _HA_BASE_URL_RETRY_COOLDOWN
        return ""


def notify_ha(mqtt_client: Any, title: str, message: str, notification_id: str) -> None:
    """Create or replace a persistent notification in Home Assistant.

    Uses the HA Supervisor REST API.  Falls back to a log warning when running
    outside the HA add-on environment (no SUPERVISOR_TOKEN).

    ``mqtt_client`` is accepted for API compatibility but not used.
    """
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
    if not supervisor_token:
        log_mqtt.warning("HA notification (no supervisor token): [%s] %s", notification_id, title)
        return

    payload = json.dumps(
        {
            "title": title,
            "message": message,
            "notification_id": notification_id,
        }
    ).encode()

    # Header key casing here is unobservable: urllib.request.Request
    # normalises every header key via str.capitalize() internally (verified
    # empirically), so "authorization"/"AUTHORIZATION"/"Authorization" all
    # resolve identically via get_header() and on the wire.
    req = urllib.request.Request(
        "http://supervisor/core/api/services/persistent_notification/create",
        data=payload,
        headers={
            "Authorization": f"Bearer {supervisor_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(
            req, timeout=10
        )  # hardcoded http://supervisor/ URL, not runtime-controllable input  # nosec B310
        log_mqtt.warning("HA notification sent: [%s] %s", notification_id, title)
    except Exception as e:  # noqa: BLE001 — must never raise; called from other exception handlers
        log_mqtt.error("Failed to send HA notification: %s", e)


def dismiss_ha(mqtt_client: Any, notification_id: str) -> None:
    """Dismiss a persistent notification in Home Assistant.

    ``mqtt_client`` is accepted for API compatibility but not used.
    """
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
    if not supervisor_token:
        log_mqtt.info("HA notification dismiss (no supervisor token): [%s]", notification_id)
        return

    payload = json.dumps({"notification_id": notification_id}).encode()
    # Header key casing here is unobservable — see the matching comment in
    # notify_ha(). The method="POST" kwarg is also unobservable: urllib's
    # Request.get_method() defaults to "POST" whenever data is set (verified
    # empirically), and data=payload is always set here — the real HTTP verb
    # sent is identical whether this kwarg is "POST", None, or dropped.
    req = urllib.request.Request(
        "http://supervisor/core/api/services/persistent_notification/dismiss",
        data=payload,
        headers={
            "Authorization": f"Bearer {supervisor_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(
            req, timeout=10
        )  # hardcoded http://supervisor/ URL, not runtime-controllable input  # nosec B310
        log_mqtt.debug("HA notification dismissed: [%s]", notification_id)
    except Exception as e:  # noqa: BLE001 — must never raise; called from other exception handlers
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
    _MAX_BACKOFF = 300  # cap at 5 minutes between reconnect attempts

    def __init__(self, entity_manager: "EntityManager", publisher: MqttDiscoveryPublisher) -> None:
        self._em = entity_manager
        self._pub = publisher
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws_lock = threading.Lock()
        self._current_ws = None
        self._msg_id = 0
        self._unique_id_map: dict = {}
        # Protects _unique_id_map. _connect_and_subscribe (watcher thread, on
        # every reconnect) reassigns the dict wholesale; refresh_registry()
        # (runs on a separate debounce-timer thread) mutates it in place.
        # Without this lock, a reconnect racing an in-flight refresh_registry()
        # call could either silently lose that refresh's updates (writes
        # landing on the old, about-to-be-discarded dict), or — worse — a
        # caller iterating the dict via resolve_point_from_entity_id() could
        # hit "RuntimeError: dictionary changed size during iteration" if a
        # concurrent write added a key mid-iteration.
        self._registry_map_lock = threading.Lock()
        # Coalesces refresh_registry() calls triggered by entity_registry_updated
        # events — see _schedule_refresh_registry() for why this exists.
        self._refresh_timer: threading.Timer | None = None
        self._refresh_timer_lock = threading.Lock()

    def start(self) -> None:
        """Start the background watcher thread."""
        supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
        if not supervisor_token:
            log_registry.debug(
                "No SUPERVISOR_TOKEN — entity registry watcher disabled "
                "(running outside HA add-on environment)"
            )
            return
        self._thread = threading.Thread(target=self._run, name="nibe_registry_watcher", daemon=True)
        self._thread.start()
        log_registry.info("Entity registry watcher started")

    def entity_id_for(self, point_id: int) -> str | None:
        """Return the HA entity_id for a Nibe point, or None if not registered.

        Uses the local cache populated from the initial registry fetch and
        live registry events. Returns None if not found.
        """
        with self._registry_map_lock:
            return self._unique_id_map.get(f"nibe_{point_id}")

    def refresh_registry(self) -> None:
        """Re-fetch the full entity registry and refresh the local cache.

        Called when entity_id_for returns None for a point that is known to
        be active — handles the case where HA registered the entity but the
        registry event was missed or had unexpected structure.
        """
        # A None/dropped default is unobservable — only ever checked via
        # `if not token:`, where None and '' are equally falsy. Verified
        # empirically.
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        if not token:
            return
        try:
            import websocket as _ws_lib

            ws = _ws_lib.create_connection(
                "ws://supervisor/core/websocket",
                timeout=10,
            )
            try:
                self._ws_authenticate(ws, token)
            except RuntimeError as e:
                # _ws_authenticate already closes ws on the failure paths
                # that raise this — closing again here would double-close.
                log_registry.warning("Registry refresh: %s", e)
                return
            # Fetch registry — close ws in a finally so a send/recv/parse
            # failure after a successful auth handshake can't leak the
            # socket (unlike the auth-failure paths above, which close it
            # themselves before raising).
            try:
                ws.send(json.dumps({"id": 1, "type": "config/entity_registry/list"}))
                raw = ws.recv()
                resp = json.loads(raw)
            finally:
                ws.close()
            if resp.get("success"):
                count = 0
                with self._registry_map_lock:
                    for entry in resp.get("result", []):
                        uid = entry.get("unique_id")
                        eid = entry.get("entity_id")
                        if uid and eid and uid.startswith("nibe_"):
                            self._unique_id_map[uid] = eid
                            count += 1
                log_registry.debug("Registry refresh: updated %d nibe entries", count)
        except Exception as e:  # noqa: BLE001 — best-effort; logged and degrades gracefully
            log_registry.warning("Registry refresh failed: %s", e)

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
                try:  # noqa: SIM105 — deliberately broad, documented on the except line below
                    self._current_ws.close()
                except Exception:  # noqa: BLE001, S110 — best-effort ws.close() during cleanup; primary error already logged  # nosec B110
                    pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        log_registry.debug("Entity registry watcher stopped")

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    @staticmethod
    def _ws_authenticate(ws: Any, token: str) -> None:
        """Authenticate a newly opened WebSocket connection to the HA Supervisor.

        Performs the three-step auth handshake expected by HA's WebSocket API:
        wait for ``auth_required``, send credentials, confirm ``auth_ok``.

        Args:
            ws: Open WebSocket connection.
            token: Supervisor access token.

        Raises:
            RuntimeError: When the greeting or auth response is unexpected.
        """
        greeting = json.loads(ws.recv())
        if greeting.get("type") != "auth_required":
            ws.close()
            raise RuntimeError(f"Unexpected WS greeting type: {greeting.get('type', 'unknown')}")
        ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth_result = json.loads(ws.recv())
        if auth_result.get("type") != "auth_ok":
            ws.close()
            raise RuntimeError(
                f"WS auth failed (response type: {auth_result.get('type', 'unknown')})"
            )

    def _connect_and_subscribe(self, token: str) -> object:
        import websocket

        ws = websocket.create_connection("ws://supervisor/core/websocket", timeout=10)

        self._ws_authenticate(ws, token)

        sub_id = self._next_id()
        ws.send(
            json.dumps(
                {
                    "id": sub_id,
                    "type": "subscribe_events",
                    "event_type": "entity_registry_updated",
                }
            )
        )
        sub_result = json.loads(ws.recv())
        if not sub_result.get("success"):
            ws.close()
            raise RuntimeError(f"Event subscription failed: {sub_result}")

        fresh_map = self._fetch_entity_registry(ws)
        with self._registry_map_lock:
            self._unique_id_map = fresh_map

        # Set a per-recv timeout equal to the ping interval so the event
        # loop wakes up regularly to send keepalive pings. Without pings,
        # a stale connection goes undetected for up to the full recv timeout.
        # _PING_INTERVAL_S drives the recv timeout; _PING_TIMEOUT_S is how
        # long to wait for a pong before treating the connection as dead.
        ws.settimeout(self._PING_INTERVAL_S)
        log_registry.debug("WebSocket connected and subscribed to entity_registry_updated events")
        return ws

    _MAX_CONSEC_FAILURES = 10
    _PING_INTERVAL_S = 30  # send a ping after this many seconds of silence
    _PING_TIMEOUT_S = 15  # reconnect if no pong arrives within this long

    def _run(self) -> None:
        """Main loop: connect → recv events → reconnect on failure.

        Gives up after _MAX_CONSEC_FAILURES consecutive connection failures
        to avoid looping forever when the supervisor WebSocket is permanently
        unavailable. The counter resets to zero on any successful connection.
        """
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        backoff = self._INITIAL_BACKOFF
        consec_failures = 0

        while not self._stop_event.is_set():
            # Only ever read via `if ws:` in the finally block below — any
            # other falsy placeholder (e.g. '') is unobservable here.
            # Verified empirically, not pragma'd since the reassignment on
            # the next line and the `if ws:` check itself are real/tested.
            ws: Any = None
            try:
                ws = self._connect_and_subscribe(token)
                with self._ws_lock:
                    self._current_ws = ws
                backoff = self._INITIAL_BACKOFF
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
                        # `> 0` vs `> 1` is unobservable here — verified
                        # empirically: ping_sent_at is either the 0.0 "no
                        # ping in flight" sentinel or a real Unix epoch
                        # timestamp (~1.7 billion), and both thresholds
                        # agree on every value either side actually takes.
                        if ping_sent_at > 0 and now - ping_sent_at > self._PING_TIMEOUT_S:
                            raise ConnectionError(
                                f"WebSocket keepalive timeout — no pong received "
                                f"in {self._PING_TIMEOUT_S}s after ping"
                            ) from None
                        ws.send(
                            json.dumps(
                                {
                                    "id": self._next_id(),
                                    "type": "ping",
                                }
                            )
                        )
                        ping_sent_at = now
                        continue

                    # Any received frame (including pong) resets ping state
                    ping_sent_at = 0.0

                    if not raw:
                        # Server-initiated clean close (websocket-client's
                        # normal signal — recv() doesn't always raise for
                        # this). Must go through the except Exception branch
                        # below like any other disconnect, not `break`
                        # straight to the outer loop — a bare break skips
                        # both the backoff wait and the consec_failures
                        # counter, so a proxy that closes cleanly on every
                        # attempt (restart loop, rate-limiting, HA Core not
                        # yet ready at startup) would spin in a zero-delay
                        # reconnect loop that can never trip
                        # _MAX_CONSEC_FAILURES and give up.
                        raise ConnectionError("WebSocket closed by server (empty recv)")
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError as e:
                        log_registry.debug("Registry watcher: discarding malformed frame: %s", e)
                        continue

                    # This match's key/value literals are unobservable:
                    # ping_sent_at is already unconditionally reset above on
                    # any successful recv, and a pong that fails this match
                    # simply falls through to the "event" check below, which
                    # also doesn't match "pong" — so _handle_event is never
                    # called either way. Verified empirically — no test
                    # distinguishes a broken match here from a working one.
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

            except Exception as e:  # noqa: BLE001 — best-effort; logged and degrades gracefully
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
                    "Registry watcher disconnected (%s) — reconnecting in %ds (failure %d/%d)",
                    e,
                    backoff,
                    consec_failures,
                    self._MAX_CONSEC_FAILURES,
                )
                self._stop_event.wait(timeout=backoff)
                backoff = min(backoff * 2, self._MAX_BACKOFF)

            finally:
                with self._ws_lock:
                    # Only ever read via `if self._current_ws:` (here and in
                    # stop()) — any other falsy placeholder is unobservable.
                    # Verified empirically, not pragma'd since the real
                    # assignment (line 508) and the truthiness checks
                    # themselves are real/tested.
                    self._current_ws = None
                if ws:
                    try:  # noqa: SIM105 — deliberately broad, documented on the except line below
                        ws.close()
                    except Exception:  # noqa: BLE001, S110 — best-effort ws.close() during cleanup; primary error already logged  # nosec B110
                        pass

        log_registry.debug("Registry watcher thread exiting")

    def _fetch_entity_registry(self, ws: Any) -> dict:
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
                    msg.get("type"),
                    msg.get("id"),
                )
        except Exception as e:  # noqa: BLE001 — best-effort; logged and degrades gracefully
            log_registry.warning("Could not fetch entity registry (timeout or error): %s", e)
            return {}
        finally:
            ws.settimeout(None)
        # resp's initial None (line 618) is unobservable here: the only way
        # to reach this line without the `except` above already returning
        # is via the loop's `break`, which always assigns a real dict to
        # resp first — so `not resp` is always False in practice, making
        # `or`/`and` and resp's initial-value literal equivalent mutants.
        # Verified empirically, not pragma'd since resp.get("success") is
        # real/tested (see test_failed_response_returns_empty_dict).
        if not resp or not resp.get("success"):
            log_registry.warning("Could not fetch entity registry: %s", resp)
            return {}
        mapping = {}
        result = resp.get("result", [])
        for entry in result:
            uid = (
                entry.get("unique_id")
                or entry.get("config", {}).get("unique_id")
                or entry.get("options", {}).get("unique_id")
            )
            eid = entry.get("entity_id")
            if uid and eid:
                mapping[uid] = eid
        nibe_count = sum(1 for k in mapping if k.startswith("nibe_"))
        log_registry.debug(
            "Entity registry cached: %d total entries, %d nibe entries",
            len(mapping),
            nibe_count,
        )
        return mapping

    def _handle_event(self, event: dict) -> None:
        """Process a single entity_registry_updated event payload."""
        data = event.get("data", {})
        action = data.get("action")
        entity_id = data.get("entity_id", "unknown")
        log_registry.debug("Registry event: action=%s, entity_id=%s", action, entity_id)

        if action == "create":
            eid = data.get("entity_id")
            uid = data.get("unique_id") or data.get("config", {}).get("unique_id")
            if uid and eid:
                with self._registry_map_lock:
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
                with self._registry_map_lock:
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
            eid = data.get("entity_id")
            uid = data.get("unique_id") or data.get("config", {}).get("unique_id")
            if uid:
                with self._registry_map_lock:
                    self._unique_id_map.pop(uid, None)
            elif eid:
                # Same fallback as the create/update branches above: if HA's
                # remove event doesn't carry unique_id (plausible — the
                # registry entry being deleted isn't necessarily echoed back
                # in full), a reverse pop by uid can't happen, and the stale
                # entry would otherwise never clear until the next full
                # refresh_registry()/reconnect. A debounced refresh rebuilds
                # the map from scratch, which naturally drops the removed
                # entity too.
                self._schedule_refresh_registry()
            return

    def _on_entity_enabled(self, ha_entity_id: str) -> None:
        """Handle a HA-side entity re-enable."""
        with self._registry_map_lock:
            unique_id_map_snapshot = dict(self._unique_id_map)
        point_id = self._em.resolve_point_from_entity_id(
            ha_entity_id, unique_id_map=unique_id_map_snapshot
        )
        if point_id is None:
            return

        log_registry.info(
            "Entity %s (point %s) re-enabled via HA — republishing discovery",
            ha_entity_id,
            point_id,
        )

        notif_id = self._em.ha_disable_notif_id(ha_entity_id)
        dismiss_ha(self._em.mqtt, notif_id)

        if point_id not in self._em.mqtt_enabled_points:
            self._em.enable_entity(point_id)
            _publish_stats(self._em, self._pub)
        else:
            point_dict = self._em.all_points_by_id.get(point_id)
            if point_dict:
                self._pub.publish_entity_discovery(point_dict, self._em.bulk_data)

        title, message, _ = self._em.build_disable_notification(
            point_id, ha_entity_id, action="re-enabled"
        )
        notify_ha(self._em.mqtt, title=title, message=message, notification_id=notif_id)

    def _on_entity_disabled(self, ha_entity_id: str) -> None:
        """Handle a HA-side entity disable."""
        with self._registry_map_lock:
            unique_id_map_snapshot = dict(self._unique_id_map)
        point_id = self._em.resolve_point_from_entity_id(
            ha_entity_id, unique_id_map=unique_id_map_snapshot
        )
        if point_id is None:
            return

        point = self._em.all_points_by_id.get(point_id)
        # Only ever consumed via `if is_dynamic:` below — None and False
        # are indistinguishable. Verified empirically.
        is_dynamic = point.get("is_dynamic", False) if point else False

        log_registry.debug(
            "Entity %s (point %s) disabled via HA — mirroring disable",
            ha_entity_id,
            point_id,
        )

        title, message, notif_id = self._em.build_disable_notification(
            point_id, ha_entity_id, action="disabled"
        )

        if is_dynamic:
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
            return  # no confusing notification for an intentional disable

        notify_ha(self._em.mqtt, title=title, message=message, notification_id=notif_id)


# ============================================================================
# MANAGEMENT COMMAND HANDLERS
# ============================================================================


class ManagementCommandHandler:
    """Registers and handles all management MQTT topics.

    Instantiate and call ``register_all()`` once after management discovery
    configs have been published.  Each handler dispatches blocking work to
    ``mgmt_executor`` so the MQTT callback thread is never held. The test
    suite runner is the one exception: it uses its own single-worker
    ``test_executor`` so a 25-30 minute run can't be starved by, or starve,
    other management commands sharing ``mgmt_executor``.
    """

    def __init__(
        self,
        mqtt_client: Any,
        entity_manager: "EntityManager",
        publisher: MqttDiscoveryPublisher,
        mgmt_executor: concurrent.futures.ThreadPoolExecutor,
        test_executor: concurrent.futures.ThreadPoolExecutor | None = None,
        ca_cert_path: str | None = None,
    ) -> None:
        self._mqtt = mqtt_client
        self._em = entity_manager
        self._pub = publisher
        self._executor = mgmt_executor
        # Only set when nibe_ca_cert is configured and the file exists —
        # mirrors _build_ssl_context()'s own check in generate_nibe_mqtt.py,
        # so the connectivity check's curl invocation verifies against the
        # same CA the bridge's own NibeApiClient actually uses, rather than
        # always skipping verification and giving a falsely reassuring
        # result for a user who has verified TLS configured.
        self._ca_cert_path = ca_cert_path
        # Dedicated executor for run_test_suite so a 25-30 minute test run
        # can never be starved by (or starve) other management commands
        # sharing mgmt_executor's fixed worker pool.
        self._test_executor = test_executor or concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="nibe_test_runner"
        )
        self._test_running = threading.Event()

    def register_all(self) -> None:
        """Subscribe to all management topics and wire up their handlers.

        Every subscription is also recorded with
        ``entity_manager.register_mgmt_subscription`` so that
        ``resubscribe_all()`` can replay it after a Mosquitto restart.
        """
        self._sub(MgmtTopic.SMART_SET, self._handle_smart_mode)
        self._sub(MgmtTopic.AID_SET, self._handle_aid_mode)
        self._sub(MgmtTopic.ALARM_RESET_PRESS, self._handle_reset_alarms)
        self._sub(MgmtTopic.FORCE_POLL_PRESS, self._handle_force_poll)
        self._sub(MgmtTopic.REGEN_DASH_PRESS, self._handle_regen_dashboard)
        self._sub(MgmtTopic.ENABLE_SET, self._handle_enable)
        self._sub(MgmtTopic.DISABLE_SET, self._handle_disable)
        self._sub(MgmtTopic.CHANGELOG_READ_PRESS, self._handle_changelog_reset)
        self._sub(MgmtTopic.FLUSH_MAP_PRESS, self._handle_flush_dynamic_map)
        self._sub(MgmtTopic.RUN_TESTS_PRESS, self._handle_run_tests)
        self._sub(MgmtTopic.TEST_CONNECTION_PRESS, self._handle_test_connection)
        self._sub(BrowserTopic.SNAPSHOTS_CMD, self._handle_snapshot_cmd)

    # ── Internal helper ───────────────────────────────────────────────────────

    def _sub(self, topic: str, handler: Callable, qos: int = 1) -> None:
        """Subscribe, add callback, and record for resubscription on reconnect."""
        self._mqtt.subscribe(topic, qos=qos)
        self._mqtt.message_callback_add(topic, handler)
        self._em.register_mgmt_subscription(topic, handler, qos)

    def _submit(self, fn: Callable) -> None:
        """Submit a handler's blocking work to mgmt_executor with logging.

        A bare ``self._executor.submit(fn)`` silently swallows any exception
        ``fn`` raises — nothing awaits the returned Future or checks
        ``.result()``, so a bug in a handler fails invisibly with no log
        line and no user-visible error. Wrapping it here ensures every
        command handler's failures are at least logged.
        """

        def _wrapped() -> None:
            try:
                fn()
            except Exception:
                log_commands.exception("Unhandled exception in management command handler")

        self._executor.submit(_wrapped)

    # ── Handlers ──────────────────────────────────────────────────────────────

    def _handle_smart_mode(self, _client: Any, _userdata: Any, message: Any) -> None:
        value = message.payload.decode().strip().lower()
        if value not in ("normal", "away"):
            log_commands.error("Invalid smart mode value: %r — expected 'normal' or 'away'", value)
            return

        def _do() -> None:
            if self._em._api.write_device_mode("smartmode", value):
                self._mqtt.publish(MgmtTopic.SMART_STATE, value, retain=True)
                # device_modes_dirty/device_modes_cache are also read+written
                # together as a pair by _publish_device_modes on the poll
                # thread — _em_lock (RLock) serializes both sides.
                # device_modes_write_seq is bumped so _publish_device_modes
                # can detect a write landing while its own fetch_device_info()
                # call is in flight (see its declaration for why).
                with self._em._em_lock:
                    self._em.device_modes_dirty = True
                    self._em.device_modes_write_seq += 1

        self._submit(_do)

    def _handle_aid_mode(self, _client: Any, _userdata: Any, message: Any) -> None:
        payload = message.payload.decode().strip()
        value = "on" if payload in ("ON", "1", "on", "true", "True") else "off"

        def _do() -> None:
            if self._em._api.write_device_mode("aidmode", value):
                self._mqtt.publish(
                    MgmtTopic.AID_STATE,
                    "ON" if value == "on" else "OFF",
                    retain=True,
                )
                with self._em._em_lock:
                    self._em.device_modes_dirty = True
                    self._em.device_modes_write_seq += 1

        self._submit(_do)

    def _handle_reset_alarms(self, _client: Any, _userdata: Any, _message: Any) -> None:
        def _do() -> None:
            if self._em._api.reset_notifications():
                self._mqtt.publish(MgmtTopic.ALARM_STATE, "0", retain=True)
                self._mqtt.publish(
                    MgmtTopic.ALARM_ATTRS,
                    json.dumps({"alarms": [], "last_updated": _fmt_ts()}),
                    retain=True,
                )

        self._submit(_do)

    def _handle_force_poll(self, _client: Any, _userdata: Any, _message: Any) -> None:
        def _do() -> None:
            log_startup.info("Force poll triggered from HA")
            self._em.update_all_states(force=True)
            update_stats_and_health(self._em, self._pub)
            _publish_device_modes(self._em, self._pub)

        self._submit(_do)

    def _handle_regen_dashboard(self, _client: Any, _userdata: Any, _message: Any) -> None:
        log_startup.info("Regenerate Dashboard triggered from HA")

        def _do() -> None:
            cb = self._em._on_enabled_state_change
            if cb is not None:
                cb()
            else:
                log_startup.warning("Regenerate Dashboard: no callback registered")

        self._submit(_do)

    def _handle_enable(self, _client: Any, _userdata: Any, message: Any) -> None:
        raw = message.payload.decode().strip()

        def _do() -> None:
            try:
                point_id = int(raw)
                if self._em.enable_entity(point_id):
                    _publish_stats(self._em, self._pub)
            except ValueError:
                log_commands.warning("handle_enable: invalid point id '%s'", raw)

        self._submit(_do)

    def _handle_disable(self, _client: Any, _userdata: Any, message: Any) -> None:
        raw = message.payload.decode().strip()

        def _do() -> None:
            try:
                point_id = int(raw)
                if self._em.disable_entity(point_id):
                    _publish_stats(self._em, self._pub)
            except ValueError:
                log_commands.warning("handle_disable: invalid point id '%s'", raw)

        self._submit(_do)

    def _handle_changelog_reset(self, _client: Any, _userdata: Any, _message: Any) -> None:
        log_history.info("Changelog reset requested by user")
        self._submit(self._em.mark_changelog_read)

    def _handle_flush_dynamic_map(self, _client: Any, _userdata: Any, _message: Any) -> None:
        log_commands.info("Flush Dynamic Map triggered from HA (debug action)")

        def _do() -> None:
            entity_types = {
                pid: pt.get("entity_type", "") for pid, pt in self._em.all_points_by_id.items()
            }
            # dynamic_point_map._table is also mutated (under _em_lock) by
            # _run_learning_detection (write executor thread) and
            # _publish_dynamic_changes (poll thread) — this runs on
            # mgmt_executor's thread, a third mutator that must serialize
            # against those two the same way, or a flush landing mid-mutation
            # on either of those threads corrupts the table rather than just
            # producing a stale read.
            with self._em._em_lock:
                self._em.dynamic_point_map.flush(self._em.all_points_by_id, entity_types)
                self._em._persist_dynamic_map()
            log_commands.info("Dynamic map flushed — all entries reset to unprocessed")

        self._submit(_do)

    def _handle_snapshot_cmd(self, _client: Any, _userdata: Any, message: Any) -> None:
        """Handle snapshot commands from the card via nibe/browser/snapshots/cmd.

        Expected payload (JSON):
            {"action": "save",    "name": "Summer Profile"}
            {"action": "restore", "name": "Summer Profile", "mode": "flush|merge"}
            {"action": "delete",  "name": "Summer Profile"}
        """
        try:
            # Python codec names are case-insensitive ('utf-8' == 'UTF-8')
            # — not pragma'd, the codec itself and the except clause below
            # are real/tested.
            cmd = json.loads(message.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            log_commands.error("snapshot_cmd: invalid payload: %s", e)
            return

        if not isinstance(cmd, dict):
            # Valid JSON but not an object (e.g. "5", null, ["x"]) — .get()
            # below would raise AttributeError uncaught on the MQTT client's
            # own thread, since this handler runs directly in the paho
            # message callback rather than the isolated command executor.
            log_commands.error("snapshot_cmd: expected a JSON object, got %r", cmd)
            return

        action = cmd.get("action", "").strip().lower()
        name = cmd.get("name", "").strip()

        def _do() -> None:
            if action == "save":
                ok, msg = self._em.save_snapshot(name)
            elif action == "restore":
                # A case-variant default (e.g. "FLUSH") is unobservable: the
                # trailing .lower() normalises it back to the real value —
                # verified empirically. Not pragma'd since 'flush' vs a
                # different-word default IS real/tested (test_restore_defaults_to_flush).
                mode = cmd.get("mode", "flush").strip().lower()
                if mode not in ("flush", "merge"):
                    log_commands.error(
                        "snapshot_cmd restore: unknown mode %r — expected 'flush' or 'merge', "
                        "using flush",
                        mode,
                    )
                    mode = "flush"
                ok, msg = self._em.restore_snapshot(name, mode)
                if ok:
                    _publish_stats(self._em, self._pub)
            elif action == "delete":
                ok, msg = self._em.delete_snapshot(name)
            else:
                log_commands.error(
                    "snapshot_cmd: unknown action %r — expected 'save', 'restore', or 'delete'",
                    action,
                )
                return
            log_commands.info("snapshot_cmd %s '%s': %s", action, name, msg)

        self._submit(_do)

    def _handle_run_tests(self, _client: Any, _userdata: Any, _message: Any) -> None:
        """Run the full pytest suite in a background thread.

        The run itself (subprocess orchestration, HTML report
        post-processing, output parsing, MQTT/notification reporting) is
        implemented in nibe_test_runner.run_test_suite — this handler is
        only responsible for MQTT command routing and the duplicate-trigger
        guard.
        """
        log_commands.info("Run Test Suite triggered from HA (debug action)")

        if self._test_running.is_set():
            log_commands.info("Test suite already running — ignoring duplicate trigger")
            return
        self._test_running.set()

        self._test_executor.submit(
            run_test_suite,
            self._em.mqtt,
            notify_ha,
            dismiss_ha,
            _get_ha_base_url,
            self._test_running,
        )

    def _handle_test_connection(self, _client: Any, _userdata: Any, _message: Any) -> None:
        """Run an independent ping + curl connectivity check against the
        configured Nibe REST API host, for diagnosing "add-on can't reach
        the device" reports without needing SSH/terminal access to the HA
        host. See nibe_connectivity_check.py for why this deliberately
        avoids reusing NibeApiClient/urllib.
        """
        log_commands.info("Test API Connection triggered from HA (debug action)")

        def _do() -> None:
            base_url = self._em._api.base_url
            host = urlparse(base_url).hostname or base_url

            self._em.mqtt.publish(MgmtTopic.TEST_CONNECTION_STATE, "running", retain=True)

            result = run_connectivity_check(
                host,
                base_url,
                self._ca_cert_path,
                self._em._api.auth,
            )

            state = "reachable" if result["ok"] else "unreachable"
            self._em.mqtt.publish(MgmtTopic.TEST_CONNECTION_STATE, state, retain=True)
            self._em.mqtt.publish(
                MgmtTopic.TEST_CONNECTION_ATTRS,
                json.dumps(
                    {
                        "status": state,
                        "summary": result["summary"],
                        "ping": result["ping"],
                        "curl": result["curl"],
                        "timestamp": _fmt_ts(),
                    }
                ),
                retain=True,
            )
            if result["ok"]:
                dismiss_ha(self._em.mqtt, "nibe_connectivity_check")
            else:
                notify_ha(
                    self._em.mqtt,
                    title="Nibe Bridge: Connectivity Check",
                    message=(
                        f"{result['summary']}\n\n"
                        f"Ping: {result['ping']['summary']}\n"
                        f"Curl: {result['curl']['summary']}"
                    ),
                    notification_id="nibe_connectivity_check",
                )

        self._submit(_do)


# ============================================================================
# POLL-LOOP HELPERS
# ============================================================================


def update_alarm_state(
    entity_manager: "EntityManager",
    publisher: MqttDiscoveryPublisher,
) -> None:
    """Fetch /notifications and update the Active Alarms sensor + HA notification."""
    if entity_manager.api_consecutive_failures > 0:
        return

    alarms = entity_manager._api.fetch_notifications()
    if alarms is None:
        log_stats.debug("Alarm poll skipped — fetch_notifications returned None (API error)")
        return

    alarm_count = len(alarms)
    clean_alarms = [
        {
            "alarmId": a.get("alarmId"),
            "header": a.get("header", ""),
            "description": a.get("description", ""),
            "severity": a.get("severity"),
            "time": a.get("time", ""),
            "equipName": a.get("equipName", ""),
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
            # `a` here iterates clean_alarms (built above with 'header' and
            # 'description' keys always present, defaulting to '' there) —
            # these two .get() defaults can never actually fire, so their
            # literal values are unobservable. Verified empirically, not
            # pragma'd since the rest of each line (equipName/severity
            # presence checks, the dedup comparison, the join) is real/tested.
            parts = [a.get("header", "Unknown alarm")]
            if a.get("equipName"):
                parts.append(f"Equipment: {a['equipName']}")
            if a.get("severity"):
                parts.append(f"Severity: {a['severity']}")
            # This default is unreachable — every test's alarm fixture
            # (_alarm()) always supplies a 'description' key explicitly
            # (default ''), so any default value here (None, dropped, or a
            # wrong truthy literal) never actually fires. Verified empirically.
            desc = a.get("description", "")
            if desc and desc != a.get("header"):
                parts.append(desc)
            lines.append(" — ".join(parts))

        device_model = entity_manager.device_info.get("model", "S-series")
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


def update_stats_and_health(
    entity_manager: "EntityManager", publisher: MqttDiscoveryPublisher
) -> None:
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
        bridge_start_time=entity_manager.bridge_start_time,
        api_consecutive_failures=entity_manager.api_consecutive_failures,
        api_failure_threshold=entity_manager.api_failure_threshold,
        api_last_success_time=entity_manager.api_last_success_time,
        last_fetch_duration=entity_manager.last_fetch_duration,
        write_total=entity_manager._write_total,
        write_success=entity_manager._write_success,
        write_failed=entity_manager._write_failed,
        last_write_error=entity_manager._last_write_error,
        pending_write_count=pending,
        mqtt_enabled_count=len(entity_manager.mqtt_enabled_points),
        all_points_count=len(entity_manager.all_points_by_id),
        known_dynamic_count=len(entity_manager.dynamic_point_map.all_known_dynamic_point_ids()),
    )


def update_device_modes(entity_manager: "EntityManager", publisher: MqttDiscoveryPublisher) -> None:
    """Poll the device API for aid/smart mode and publish their states."""
    _publish_device_modes(entity_manager, publisher)


# ── Private helpers ────────────────────────────────────────────────────────────


def _publish_stats(entity_manager: "EntityManager", publisher: MqttDiscoveryPublisher) -> None:
    with entity_manager._active_entities_lock:
        active_count = len(entity_manager.active_entities_by_id)

    publisher.publish_stats(
        all_points_count=len(entity_manager.all_points_by_id),
        mqtt_enabled_count=len(entity_manager.mqtt_enabled_points),
        active_count=active_count,
        type_counts=dict(entity_manager._stats_type_counts),
        category_counts=dict(entity_manager._stats_category_counts),
        writable_count=entity_manager._stats_writable_count,
        write_total=entity_manager._write_total,
        write_success=entity_manager._write_success,
        write_failed=entity_manager._write_failed,
    )
    mqtt_count = len(entity_manager.mqtt_enabled_points)
    total_count = len(entity_manager.all_points_by_id)
    stats_key = (mqtt_count, active_count, total_count)
    if getattr(entity_manager, "_last_stats_key", None) != stats_key:
        log_stats.debug(
            "Stats: MQTT=%d, Active=%d, Total=%d",
            mqtt_count,
            active_count,
            total_count,
        )
        entity_manager._last_stats_key = stats_key


def _publish_device_modes(
    entity_manager: "EntityManager", publisher: MqttDiscoveryPublisher
) -> None:
    """Publish aid mode and smart mode states.

    Uses a cache to avoid an extra fetch_device_info() API call on every
    poll cycle.  The cache is marked dirty on startup and after any write
    to either mode, so the next call always fetches fresh values when needed.
    """
    if entity_manager.api_consecutive_failures > 0:
        return

    # dirty+cache are read together, and cache/dirty are written together
    # below — _em_lock (RLock) keeps both sides consistent against the
    # command-handler threads that set device_modes_dirty on a write.
    with entity_manager._em_lock:
        if not entity_manager.device_modes_dirty and entity_manager.device_modes_cache:
            cached_aid = entity_manager.device_modes_cache.get("aidMode", "off")
            cached_smart = entity_manager.device_modes_cache.get("smartMode", "normal")
            # Only ever consumed via `if not fresh:` below, so None and
            # False are indistinguishable here — not pragma'd, `fresh = True`
            # on the other branch is real/tested.
            fresh = False
        else:
            fresh = True
        # Captured before the unlocked fetch below — see device_modes_write_seq's
        # declaration in EntityManager.__init__ for why this matters: a write
        # handler on another thread can set dirty=True (and bump this seq)
        # while fetch_device_info() is in flight, meaning the response we're
        # about to get may predate that write. Blindly clearing dirty=False
        # afterward would clobber the writer's dirty=True with stale data,
        # leaving HA showing the pre-write mode until another write happens
        # to re-dirty the cache.
        write_seq_before = entity_manager.device_modes_write_seq

    if not fresh:
        publisher.publish_device_modes(aid_mode=cached_aid, smart_mode=cached_smart)
        return

    response = entity_manager._api.fetch_device_info()
    if not response:
        log_commands.warning(
            "Could not fetch device mode states — aid/smart mode display may be stale "
            "(see API errors above for the cause)"
        )
        return

    with entity_manager._em_lock:
        aid_mode = response.get("aidMode", "off")
        smart_mode = response.get("smartMode", "normal")
        if entity_manager.device_modes_write_seq == write_seq_before:
            # No concurrent write landed during the fetch — safe to cache
            # and clear dirty.
            entity_manager.device_modes_cache = {
                "aidMode": aid_mode,
                "smartMode": smart_mode,
            }
            entity_manager.device_modes_dirty = False
        # else: a write raced this fetch and already set dirty=True for us
        # (with a bumped write_seq) — leave dirty/cache alone so the next
        # poll re-fetches, rather than overwriting the writer's dirty flag
        # with a response that may predate the write.

    publisher.publish_device_modes(aid_mode=aid_mode, smart_mode=smart_mode)
