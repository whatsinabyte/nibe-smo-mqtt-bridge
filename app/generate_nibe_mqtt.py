#!/usr/bin/env python3
"""
Nibe SMO S40 → Home Assistant MQTT bridge.

Architecture overview
---------------------
The bridge polls the SMO S40 local REST API and publishes Home Assistant
MQTT discovery configs so that heat-pump data points appear automatically
as HA entities without any manual YAML configuration.

Module structure
----------------
generate_nibe_mqtt.py   ← this file: config, startup, poll loop only
nibe_api.py             ← NibeApiClient: all HTTP to the Nibe device
nibe_entity_detection.py← pure classification functions and lookup tables
nibe_mqtt_publisher.py  ← MqttDiscoveryPublisher: all MQTT topic/publish logic
nibe_entity_manager.py  ← EntityManager: point registry, enable/disable, polling
nibe_ha_integration.py  ← notifications, registry watcher, management handlers

Design decisions
----------------
MQTT-first state
    Retained discovery configs in the broker are the single source of truth
    for which entities are enabled.  On restart the bridge reads these back
    via scan_mqtt_discovery() rather than keeping a separate state file.

Entity type detection strategy
    The Nibe firmware metadata is too ambiguous for reliable auto-detection
    of entity types.  ENTITY_TYPE_OVERRIDES (in nibe_entity_detection.py) is
    the authoritative override table.  binary_sensor in particular CANNOT be
    auto-detected — a point is only classified as binary_sensor after the
    developer has confirmed it in the HA UI.

Dynamic points
    Some Nibe operating modes expose extra data points only while active.
    The bridge detects these by comparing each bulk-fetch response to a
    baseline set captured at startup and auto-enables them.

Threading model
    The main thread runs a sleep loop that fires update_all_states() every
    bulk_interval seconds.  MQTT callbacks run on paho's network thread.
    Write commands are dispatched to a single-worker ThreadPoolExecutor.
    A dedicated daemon thread (HAEntityRegistryWatcher) holds a long-lived
    WebSocket to the HA Core API.
"""

import argparse
import atexit
import base64
import concurrent.futures
import datetime
import json
import logging
import os
import re
import signal
import ssl
import sys
import time
import threading
import urllib.error
import yaml

try:
    import paho.mqtt.client as mqtt
except ImportError:
    # Use proper logging instead of print
    logging.getLogger("nibe.startup").error(
        "paho-mqtt library not found. Install with: pip install paho-mqtt"
    )
    sys.exit(1)



from nibe_api             import NibeApiClient
from nibe_entity_manager  import EntityManager, _build_device_info, decide_startup_action
from nibe_entity_detection import MODES
from nibe_mqtt_publisher  import MqttDiscoveryPublisher, MgmtTopic, MGMT_AVAIL_TOPIC
from nibe_ha_integration  import (
    HAEntityRegistryWatcher,
    ManagementCommandHandler,
    dismiss_ha,
    notify_ha,
    update_alarm_state,
    update_device_modes,
    update_stats_and_health,
)
from nibe_lovelace        import (
    build_menu_points,
    copy_card_file,
    provision_lovelace_ui,
    schedule_menu_dashboard_regen,
    remove_menu_dashboard,
    teardown_lovelace,
    _build_point_to_menu,
)

# ============================================================================
# BRIDGE VERSION
# ============================================================================
BRIDGE_VERSION = "1.0.1"
# Keep in sync with version: in config.yaml — test_bridge_version_matches_config_yaml
# in the test suite catches any mismatch automatically.

# ============================================================================
# CONFIGURATION
# ============================================================================

from dataclasses import dataclass, field  # noqa: E402


@dataclass
class BridgeConfig:
    """Fully resolved bridge configuration.

    Constructed by ``load_config()`` after merging all sources in priority order
    (CLI > env > options.json > secrets.yaml > defaults).  Using a dataclass
    rather than a raw dict gives:

    * Type-checked fields — typos in field names are caught at construction time.
    * IDE auto-complete and jump-to-definition for every config field.
    * Easy unit testing — construct ``BridgeConfig(api_host="10.0.0.1", ...)``
      directly in tests without touching the filesystem or env vars.
    * ``__repr__`` for free (useful in logs; credentials are Optional so they
      show as ``None`` when absent rather than leaking).
    """

    # Connection
    api_host:   str = "192.168.2.201"
    api_port:   int = 8443
    mqtt_broker: str = "core-mosquitto"
    mqtt_port:  int = 1883

    # Credentials
    mqtt_username:   str | None = None
    mqtt_password:   str | None = None
    nibe_username:   str | None = None
    nibe_password:   str | None = None
    nibe_basic_auth: str | None = None

    # Identity
    device_name: str = "Nibe SMO S40"
    device_id:   str = "nibe_heatpump_001"

    # Behaviour
    poll_interval:        int = 30
    log_level:            str = "info"
    mode:                 str = "essential"
    api_failure_threshold: int = 3
    changelog_retention_days: int = 90

    # TLS — optional CA certificate for verifying the Nibe device's self-signed cert.
    # When set, TLS verification is fully enabled against this CA.
    # When unset (default), verification is disabled with a startup warning.
    nibe_ca_cert: str | None = None

    # MQTT TLS — optional; set mqtt_tls=True and optionally mqtt_ca_cert to
    # encrypt broker traffic and protect MQTT credentials in transit.
    mqtt_tls:    bool         = False
    mqtt_ca_cert: str | None = None

    # Derived — populated by load_config(), not set by callers
    api_base_url: str = ""
    nibe_auth:    str | None = None

    # Deferred log warnings collected before logging was ready
    warnings: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        """Return a log-safe representation — all credential fields are redacted."""
        def _mask(val: str | None) -> str:
            return "***" if val else "None"
        return (
            f"BridgeConfig("
            f"api={self.api_host}:{self.api_port}, "
            f"mqtt={self.mqtt_broker}:{self.mqtt_port}, "
            f"mqtt_user={_mask(self.mqtt_username)}, "
            f"mqtt_password={_mask(self.mqtt_password)}, "
            f"nibe_user={_mask(self.nibe_username)}, "
            f"nibe_password={_mask(self.nibe_password)}, "
            f"nibe_basic_auth={_mask(self.nibe_basic_auth)}, "
            f"nibe_auth={_mask(self.nibe_auth)}, "
            f"device={self.device_name!r}, "
            f"mode={self.mode!r}, "
            f"poll={self.poll_interval}s"
            f")"
        )


def load_config(cli_args=None) -> BridgeConfig:
    """Resolve runtime configuration from all sources into a BridgeConfig.

    Sources are applied lowest-to-highest priority — each step unconditionally
    overwrites what came before, so no guard logic is needed anywhere.

    Priority (highest wins):
      4. CLI arguments  — log level and mode only (used by run.sh)
      3. Environment variables — non-credential settings for development/Docker
      2. options.json   — standard HA add-on configuration (primary source)
      1. secrets.yaml   — lowest priority; sets only what options.json omits
         (nibe_basic_auth and mqtt credentials)

    Credentials (nibe_username/password, mqtt_username/password) are only
    read from options.json and secrets.yaml — not from environment variables,
    to avoid credentials leaking into process listings or Docker inspect output.
    """
    cfg = BridgeConfig()
    deferred_warnings: list[str] = []

    _VALID_POLL_INTERVALS = {15, 30, 60, 120, 300}

    def _opt_str(options: dict, key: str) -> str | None:
        v = options.get(key)
        return str(v) if v else None

    def _opt_int(options: dict, key: str) -> int | None:
        v = options.get(key)
        return int(str(v)) if v is not None else None

    def _validated_poll(value: int, source: str) -> int:
        """Clip poll interval to nearest valid value and warn if not exact."""
        if value in _VALID_POLL_INTERVALS:
            return value
        clipped = min(_VALID_POLL_INTERVALS, key=lambda x: abs(x - value))
        deferred_warnings.append(
            f"{source}: poll_interval={value} is not a valid value "
            f"{sorted(_VALID_POLL_INTERVALS)} — using {clipped}s"
        )
        return clipped

    # Sources are applied lowest-to-highest priority so each later step
    # unconditionally overwrites what came before — no guard logic needed.
    #
    # Priority (highest wins):
    #   4 (last)  CLI arguments
    #   3         Environment variables
    #   2         options.json  — primary HA add-on UI source
    #   1 (first) secrets.yaml — lowest priority, sets only what options.json omits

    # ── 1. secrets.yaml — credentials only ────────────────────────────────
    # Supports nibe_basic_auth (pre-encoded Basic token) and mqtt credentials.
    # Values may contain any character including '#' — quoted values have
    # their surrounding quotes stripped; unquoted values are read to end of line.
    for path in ['/config/secrets.yaml', '/homeassistant/secrets.yaml', './secrets.yaml']:
        try:
            if not os.path.exists(path):
                continue
            with open(path) as f:
                _secrets_content = f.read()

            def _yaml_val(key, _src=_secrets_content):
                # Match: key: "value" or key: 'value' or key: bare_value
                # Quoted: captures everything between the quotes (allows # and spaces).
                # Unquoted: captures to end of line (no comment stripping — a bare
                # value containing # is taken literally, which is correct YAML
                # behaviour for scalars that are not followed by whitespace+#).
                m = re.search(
                    rf'^{re.escape(key)}:\s*'
                    rf'(?:"([^"]*)"'        # double-quoted value
                    rf"|'([^']*)'"          # single-quoted value
                    rf'|([^\n]*))',          # bare value (rest of line)
                    _src, re.MULTILINE,
                )
                if not m:
                    return None
                # Return whichever capture group matched, stripped of whitespace.
                return (m.group(1) or m.group(2) or m.group(3) or '').strip() or None

            cfg.mqtt_username   = _yaml_val('mqtt_user')      or cfg.mqtt_username
            cfg.mqtt_password   = _yaml_val('mqtt_password')  or cfg.mqtt_password
            cfg.nibe_basic_auth = _yaml_val('nibe_basic_auth') or cfg.nibe_basic_auth
            break
        except Exception as e:
            deferred_warnings.append(f"Could not read secrets file {path}: {e}")

    # ── 2. HA add-on options.json ──────────────────────────────────────────
    try:
        if os.path.exists('/data/options.json'):
            with open('/data/options.json') as f:
                opts = json.load(f)
            cfg.api_host      = _opt_str(opts, 'nibe_host')     or cfg.api_host
            cfg.api_port      = _opt_int(opts, 'nibe_port')     or cfg.api_port
            cfg.mqtt_broker   = _opt_str(opts, 'mqtt_host')     or cfg.mqtt_broker
            cfg.mqtt_port     = _opt_int(opts, 'mqtt_port')     or cfg.mqtt_port
            cfg.mqtt_username = _opt_str(opts, 'mqtt_username') or cfg.mqtt_username
            cfg.mqtt_password = _opt_str(opts, 'mqtt_password') or cfg.mqtt_password
            cfg.nibe_username = _opt_str(opts, 'nibe_username') or cfg.nibe_username
            cfg.nibe_password = _opt_str(opts, 'nibe_password') or cfg.nibe_password
            cfg.device_name   = _opt_str(opts, 'device_name')   or cfg.device_name
            cfg.log_level     = _opt_str(opts, 'log_level')     or cfg.log_level
            cfg.mode          = _opt_str(opts, 'mode')          or cfg.mode
            if opts.get('poll_interval'):
                cfg.poll_interval = _validated_poll(
                    int(opts['poll_interval']), "options.json"
                )
            if opts.get('api_failure_threshold'):
                cfg.api_failure_threshold = max(1, int(opts['api_failure_threshold']))
            if opts.get('changelog_retention_days'):
                cfg.changelog_retention_days = max(1, int(opts['changelog_retention_days']))
            if opts.get('nibe_ca_cert'):
                cfg.nibe_ca_cert = str(opts['nibe_ca_cert'])
            if opts.get('mqtt_tls') is True:
                cfg.mqtt_tls = True
            if opts.get('mqtt_ca_cert'):
                cfg.mqtt_ca_cert = str(opts['mqtt_ca_cert'])
    except Exception as e:
        deferred_warnings.append(f"Could not read /data/options.json: {e}")

    # ── 3. Environment variables — non-credential settings only ───────────
    # Credentials are intentionally excluded: env vars appear in process
    # listings and Docker inspect output, making them unsuitable for secrets.
    # Exception: NIBE_MQTT_SVC_* variables are set by run.sh from the
    # Supervisor Services API — these are Supervisor-injected values, not
    # user-provided secrets, and are treated as infrastructure plumbing.
    env = os.environ
    if env.get('NIBE_API_HOST'):    cfg.api_host    = env['NIBE_API_HOST']     # noqa: E701
    if env.get('NIBE_API_PORT'):    cfg.api_port    = int(env['NIBE_API_PORT']) # noqa: E701
    if env.get('NIBE_MQTT_BROKER'): cfg.mqtt_broker = env['NIBE_MQTT_BROKER']  # noqa: E701
    if env.get('NIBE_MQTT_PORT'):   cfg.mqtt_port   = int(env['NIBE_MQTT_PORT'])# noqa: E701
    if env.get('NIBE_MQTT_SVC_USERNAME'): cfg.mqtt_username = env['NIBE_MQTT_SVC_USERNAME']  # noqa: E701
    if env.get('NIBE_MQTT_SVC_PASSWORD'): cfg.mqtt_password = env['NIBE_MQTT_SVC_PASSWORD']  # noqa: E701
    if env.get('NIBE_DEVICE_NAME'): cfg.device_name = env['NIBE_DEVICE_NAME']  # noqa: E701
    if env.get('NIBE_LOG_LEVEL'):   cfg.log_level   = env['NIBE_LOG_LEVEL']    # noqa: E701
    if env.get('NIBE_MODE'):        cfg.mode        = env['NIBE_MODE']          # noqa: E701
    if env.get('NIBE_POLL_INTERVAL'):
        cfg.poll_interval = _validated_poll(
            max(15, int(env['NIBE_POLL_INTERVAL'])), "NIBE_POLL_INTERVAL"
        )
    if env.get('NIBE_API_FAILURE_THRESHOLD'):
        cfg.api_failure_threshold = max(1, int(env['NIBE_API_FAILURE_THRESHOLD']))

    # ── 4. CLI arguments — log level and mode only ───────────────────────
    # run.sh passes --log-level and --mode; other settings come from
    # options.json so the add-on UI remains the single source of truth.
    if cli_args:
        if getattr(cli_args, 'log_level', None): cfg.log_level = cli_args.log_level  # noqa: E701
        if getattr(cli_args, 'mode',      None): cfg.mode      = cli_args.mode        # noqa: E701

    # ── Derived values ─────────────────────────────────────────────────────
    cfg.api_base_url = f"https://{cfg.api_host}:{cfg.api_port}/api/v1/devices/0"

    if cfg.nibe_basic_auth:
        token = cfg.nibe_basic_auth
        cfg.nibe_auth = token if token.startswith('Basic ') else f"Basic {token}"
    elif cfg.nibe_username and cfg.nibe_password:
        token = base64.b64encode(
            f"{cfg.nibe_username}:{cfg.nibe_password}".encode()
        ).decode()
        cfg.nibe_auth = f"Basic {token}"

    cfg.warnings = deferred_warnings
    return cfg


# ============================================================================
# LOGGING
# ============================================================================

def _build_logging(level: str = "info") -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    root    = logging.getLogger("nibe")
    if root.handlers:
        root.setLevel(numeric)
        return
    root.setLevel(numeric)
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)

    class _Formatter(logging.Formatter):
        def format(self, record):
            ct = datetime.datetime.fromtimestamp(record.created)
            ts = ct.strftime("%H:%M:%S") + f".{ct.microsecond // 1000:03d}"
            return f"{ts} [{record.levelname:<8}] {record.name}: {record.getMessage()}"

    handler.setFormatter(_Formatter())
    root.addHandler(handler)


log_api      = logging.getLogger("nibe.api")
log_mqtt     = logging.getLogger("nibe.mqtt")
log_restore  = logging.getLogger("nibe.restore")
log_startup  = logging.getLogger("nibe.startup")
log_entities = logging.getLogger("nibe.entities")

_ALARM_POLL_INTERVAL = 10   # seconds — fixed, not user-configurable
_SHUTDOWN_TIMEOUT    = 35   # seconds to wait for executors on clean shutdown
                            # (slightly longer than the 30s API request timeout)


# ============================================================================
# CLI ARGUMENT PARSING
# ============================================================================

def parse_arguments():
    parser = argparse.ArgumentParser(description='Nibe S-Series MQTT Bridge')
    parser.add_argument('-l', '--log-level',
                        choices=['debug', 'info', 'warning', 'error'],
                        default='info', dest='log_level')
    parser.add_argument('-m', '--mode',
                        choices=['essential', 'monitoring', 'advanced', 'menus', 'all', 'none'],
                        default='essential')
    return parser.parse_args()


# ============================================================================
# CARD FILE HELPERS
# ============================================================================

def _cleanup_mqtt_retained(mqtt_client) -> None:
    """Remove all retained MQTT messages published by this bridge.

    The bridge owns two topic namespaces:
      homeassistant/+/+/+         — per-entity discovery configs, states,
                                   attributes, and availability topics.
                                   All bridge entity IDs start with "nibe_"
                                   and are filtered in _on_retained.
      nibe/browser/#             — point metadata, enabled state, changelog,
                                   dynamic events, and scan sentinel.

    Deletion is done by publishing an empty (zero-byte) retained payload to
    each topic, which is the standard MQTT mechanism for clearing a retained
    message.  A wildcard subscription is used to collect the current set of
    retained topics before clearing them, using the same sentinel pattern as
    scan_mqtt_discovery() to reliably detect end-of-retained-messages.

    Parameters
    ----------
    mqtt_client :
        The connected paho MQTT client instance.
    """
    _SCAN_TIMEOUT = 15
    _SENTINEL     = "nibe/browser/scan_sentinel"

    log_startup.info("Collecting retained MQTT topics for cleanup...")
    retained_topics = set()
    sentinel_received = threading.Event()

    def _on_retained(_client, _userdata, message):
        topic = message.topic
        if not message.retain or not message.payload or topic == _SENTINEL:
            return
        # Only collect topics that belong to this bridge.
        # Bridge HA topics follow the pattern homeassistant/<domain>/nibe_<id>/<suffix>
        # so the third segment always starts with "nibe_".
        # The nibe/browser/# namespace is always ours so no filter needed there.
        parts = topic.split('/')
        if topic.startswith("homeassistant/") and (len(parts) < 3 or not parts[2].startswith("nibe_")):
            return
        retained_topics.add(topic)

    def _on_sentinel(_client, _userdata, _message):
        sentinel_received.set()

    # Subscribe to both bridge namespaces with wildcards.
    # Note: MQTT wildcards must match complete topic levels — "nibe_+" is not
    # valid. We use "homeassistant/+/+/+" to collect all discovery topics and
    # then only clear the ones that belong to this bridge (unique_id starts
    # with "nibe_"), relying on the payload filter in _on_retained.
    ha_wildcard      = "homeassistant/+/+/+"
    browser_wildcard = "nibe/browser/#"

    mqtt_client.subscribe(ha_wildcard)
    mqtt_client.subscribe(browser_wildcard)
    mqtt_client.message_callback_add(ha_wildcard,      _on_retained)
    mqtt_client.message_callback_add(browser_wildcard, _on_retained)
    mqtt_client.subscribe(_SENTINEL)
    mqtt_client.message_callback_add(_SENTINEL, _on_sentinel)

    # Sentinel flush: any retained messages arrive before this non-retained one
    mqtt_client.publish(_SENTINEL, "cleanup", retain=False)

    if not sentinel_received.wait(timeout=_SCAN_TIMEOUT):
        log_startup.warning(
            "Sentinel timeout after %ds during MQTT cleanup — "
            "some retained messages may not have been collected", _SCAN_TIMEOUT
        )

    # Tear down callbacks and subscriptions
    mqtt_client.message_callback_remove(ha_wildcard)
    mqtt_client.message_callback_remove(browser_wildcard)
    mqtt_client.message_callback_remove(_SENTINEL)
    mqtt_client.unsubscribe(ha_wildcard)
    mqtt_client.unsubscribe(browser_wildcard)
    mqtt_client.unsubscribe(_SENTINEL)

    if not retained_topics:
        log_startup.info("No retained MQTT messages found to clean up")
        return

    log_startup.info("Clearing %d retained MQTT topics...", len(retained_topics))
    pending = []
    for topic in retained_topics:
        result = mqtt_client.publish(topic, payload=None, retain=True)
        pending.append((topic, result))

    # Wait for publishes to confirm — best-effort, 2s per message
    cleared = 0
    for topic, result in pending:
        try:
            result.wait_for_publish(timeout=2.0)
            cleared += 1
            log_startup.debug("Cleared retained topic: %s", topic)
        except Exception as e:
            log_startup.warning("Could not confirm clear for %s: %s", topic, e)

    log_startup.info("MQTT cleanup complete — cleared %d/%d retained topics",
                     cleared, len(retained_topics))


# ===========================================================================
# Startup helpers — extracted from main() for testability
# ===========================================================================

def _build_ssl_context(ca_cert_path: str | None) -> ssl.SSLContext:
    """Build an SSL context for the Nibe API connection.

    When *ca_cert_path* points to an existing file, full chain verification
    is enabled against that CA.  Otherwise a permissive context is returned
    that accepts self-signed certificates — the only practical default for
    a local device with no trusted CA.
    """
    if ca_cert_path and os.path.exists(ca_cert_path):
        ctx = ssl.create_default_context(cafile=ca_cert_path)
        log_startup.info(
            "Nibe API TLS: verification enabled using CA cert %s", ca_cert_path
        )
        return ctx

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
    ctx.set_ciphers('DEFAULT@SECLEVEL=1')
    log_startup.warning(
        "TLS: Certificate verification disabled (self-signed cert). "
        "Enable verification by setting 'nibe_ca_cert' in add-on options."
    )
    return ctx


def _derive_device_id(response: dict, fallback: str) -> str:
    """Derive a stable HA-safe device identifier from the API response.

    Uses the controller's serial number so two bridges running against
    different controllers on the same broker produce distinct identifiers.
    Falls back to *fallback* (the config default) when the serial is absent
    — e.g. when the API was unreachable at startup.
    """
    serial = (response.get("product", {}).get("serialNumber") or "").strip()
    if serial:
        safe = "".join(c for c in serial.lower() if c.isalnum() or c == "_")
        device_id = f"nibe_{safe}"
        log_startup.info("Device ID derived from serial number: %s", device_id)
        return device_id
    log_startup.warning(
        "Serial number not available — using default device_id '%s'. "
        "Running two bridges without serial numbers may cause HA device collisions.",
        fallback,
    )
    return fallback


def _resolve_initial_mode(args, cfg) -> str:
    """Return the effective entity mode: CLI flag takes priority over config."""
    return args.mode if args.mode else cfg.mode


def _build_mqtt_client_id(device_id: str) -> str:
    """Return an MQTT client ID derived from device_id, capped at 23 chars.

    23 chars satisfies conservative MQTT 3.1 broker limits; modern brokers
    accept much longer IDs but the cap ensures compatibility.
    """
    return device_id[:23]


def _configure_mqtt_tls(mqtt_client, cfg) -> None:
    """Apply TLS settings to *mqtt_client* according to *cfg*.

    Four cases:
    - TLS on  + CA cert file exists  → tls_set with the CA cert path
    - TLS on  + no CA cert           → tls_set with system CA store
    - TLS off + credentials present  → warning (plaintext credentials)
    - TLS off + no credentials       → silent (nothing to protect)
    """
    if cfg.mqtt_tls:
        ca = (
            cfg.mqtt_ca_cert
            if (cfg.mqtt_ca_cert and os.path.exists(cfg.mqtt_ca_cert))
            else None
        )
        mqtt_client.tls_set(ca_certs=ca)
        log_mqtt.info(
            "MQTT TLS enabled%s",
            f" (CA: {ca})" if ca else " (system CA store)",
        )
    else:
        if cfg.mqtt_username:
            log_mqtt.warning(
                "MQTT TLS disabled — credentials sent in plaintext. "
                "Enable with 'mqtt_tls: true' in add-on options."
            )


def _run_scan_with_retry(
    entity_manager,
    retries: int = 3,
    backoffs: list[int] | None = None,
) -> set[int]:
    """Scan MQTT for retained discovery configs, retrying on empty results.

    Returns immediately when the scan finds at least one config.  On failure
    (empty result) waits *backoffs[attempt]* seconds before retrying, up to
    *retries* total attempts.  Always returns a set (possibly empty).
    """
    if backoffs is None:
        backoffs = [3, 6, 12]

    result = entity_manager.scan_mqtt_discovery()
    for attempt, wait in enumerate(backoffs, start=1):
        if result:
            break
        if attempt >= retries:
            break
        log_restore.warning(
            "Scan returned 0 configs (attempt %d/%d) — broker may still be "
            "loading. Retrying in %ds...", attempt, retries, wait,
        )
        time.sleep(wait)
        result = entity_manager.scan_mqtt_discovery()
    return result


def _execute_startup_action(
    entity_manager,
    startup_action: str,
    applied_mode: str | None,
    initial_mode: str,
    mqtt_client,
    device_name: str,
) -> None:
    """Execute the startup action determined by decide_startup_action().

    Logs the context-specific startup message for each action, then delegates
    the actual mutations (apply_mode / restore_from_mqtt / record_applied_mode)
    to entity_manager._apply_startup_action() — the shared implementation also
    used by complete_deferred_discovery().

    apply     — fresh install: enable the configured mode.
    restore   — same mode as before: restore entities from MQTT database.
    reconcile — mode changed: restore then apply new mode to reconcile set.
    """
    if startup_action == "apply":
        log_restore.info(
            "No existing MQTT configs — applying initial mode: %s", initial_mode
        )
    elif startup_action == "restore":
        pass   # _apply_startup_action logs the applied-mode baseline message if needed
    else:  # "reconcile"
        log_restore.info(
            "Entity mode changed from '%s' to '%s' — restoring then reconciling "
            "the enabled set to the new mode.",
            applied_mode, initial_mode,
        )

    entity_manager._apply_startup_action(startup_action, applied_mode, initial_mode)

    # mode=none notification is a normal-startup-only concern — not replicated
    # in complete_deferred_discovery because deferred startups only reach this
    # path after the device comes back online and real points are discovered.
    if startup_action == "apply" and initial_mode == "none":
        log_restore.info(
            "Mode 'none' selected — no entities enabled by default. "
            "Use the Entity Manager card to enable entities."
        )
        notify_ha(
            mqtt_client,
            title="Nibe Bridge: No Entities Enabled",
            message=(
                f"{len(entity_manager.all_points)} data points were discovered on "
                f"{device_name} but none are enabled because the mode is set to "
                "'none'. No entities will appear in Home Assistant until you enable "
                "some. Use the Entity Manager card on the Nibe Bridge dashboard to "
                "enable a set of entities."
            ),
            notification_id="nibe_no_entities",
        )


def _keepalive_from_config(poll_interval: int) -> int:
    """Calculate MQTT keepalive from the poll interval.

    Keepalive must be longer than the poll interval so the broker does not
    disconnect between polls. Capped at a minimum of 60s for brokers that
    enforce a floor.
    """
    return max(60, poll_interval + 10)


class _ApiAuthError(Exception):
    """Raised by _fetch_api_response when the Nibe API rejects credentials."""


def _fetch_api_response(api_client) -> dict:
    """Fetch device info from the Nibe API and return the response dict.

    Returns an empty dict when the device is unreachable (offline at startup
    is acceptable — the bridge will retry in the poll loop).

    Raises _ApiAuthError when the API returns HTTP 401/403 — the caller
    should log the error and exit, since bad credentials will not fix
    themselves.
    """
    log_startup.info("Testing Nibe API connection...")
    try:
        response = api_client.fetch_device_info()
    except urllib.error.HTTPError as e:
        raise _ApiAuthError(e.code) from e

    if not response:
        log_startup.warning(
            "Cannot reach Nibe API at startup — device may be offline. "
            "The bridge will start and keep retrying."
        )
        return {}

    product = response.get("product", {})
    log_startup.info(
        "Connected to %s %s (serial: %s, firmware: %s)",
        product.get("manufacturer", "NIBE"),
        product.get("name", "S-series") or "S-series",
        product.get("serialNumber", "unknown"),
        product.get("firmwareId", "unknown"),
    )
    return response


def _load_menu_structure(app_dir: str, log_if_mode: bool = True) -> tuple[dict, frozenset]:
    """Load menu_structure.yaml and return (point_to_menu_map, menu_points).

    Returns ({}, frozenset()) on any error so callers can always unpack
    safely.  Errors are logged at WARNING level — a missing YAML degrades
    the menus dashboard but does not prevent the bridge from running.

    log_if_mode: suppress verbose build logs unless mode == 'menus'.
    The structure is always built — it's needed for runtime regardless of mode.
    """
    try:
        menu_path = os.path.join(app_dir, "menu_structure.yaml")
        with open(menu_path, encoding="utf-8") as f:
            menu_data = yaml.safe_load(f)
        point_to_menu = _build_point_to_menu(menu_data.get("menus", []))
        menu_points   = build_menu_points(menu_path)
        if log_if_mode:
            log_startup.debug("Built point→menu map: %d entries", len(point_to_menu))
            log_startup.debug("MODES['menus'] populated: %d points", len(menu_points))
        return point_to_menu, menu_points
    except Exception as e:
        log_startup.warning(
            "Could not build point→menu map / MODES['menus']: %s", e
        )
        return {}, frozenset()


# ============================================================================
# INFRASTRUCTURE — build API client, MQTT client, establish connections
# ============================================================================

def _build_infrastructure(
    cfg: BridgeConfig,
) -> tuple:
    """Build and connect all external-facing clients.

    Returns
    -------
    tuple of (api_client, mqtt_client, response, device_id, shutting_down, set_entity_manager)
        api_client          – NibeApiClient ready for polling
        mqtt_client         – paho client, connected, loop_start() called
        response            – device-info dict from the Nibe API (may be {})
        device_id           – serial-derived HA-safe identifier
        shutting_down       – single-element list[bool]; set True before
                              disconnect() so on_disconnect suppresses warnings
        set_entity_manager  – callable(em) → None; call once after EntityManager
                              is constructed to wire it into the on_connect
                              reconnection callback

    Calls sys.exit(1) on unrecoverable errors (bad credentials, broker
    unreachable) — identical behaviour to the previous monolithic main().
    """
    if not cfg.nibe_auth:
        log_api.error("Could not find Nibe API credentials in any source.")
        log_api.error("  Add-on: set nibe_username + nibe_password in the add-on options UI")
        log_api.error("  secrets.yaml: add  nibe_basic_auth: <base64token>")
        sys.exit(1)

    ssl_context = _build_ssl_context(cfg.nibe_ca_cert)
    api_client  = NibeApiClient(cfg.api_base_url, cfg.nibe_auth, ssl_context)

    log_startup.info("Bridge version: %s", BRIDGE_VERSION)
    copy_card_file()
    log_startup.info(
        "Config: API=%s  MQTT=%s:%d  device='%s'",
        cfg.api_base_url, cfg.mqtt_broker, cfg.mqtt_port, cfg.device_name,
    )

    # ── Test API connection ───────────────────────────────────────────────────
    try:
        response = _fetch_api_response(api_client)
    except _ApiAuthError as e:
        log_startup.error(
            "Nibe API authentication failed (HTTP %s) — check credentials.", e
        )
        sys.exit(1)

    device_id = _derive_device_id(response, cfg.device_id)

    # ── MQTT client ───────────────────────────────────────────────────────────
    # device_id is serial-based so it is unique per physical controller.
    # Truncated to 23 chars to satisfy conservative MQTT 3.1 broker limits.
    mqtt_client_id = _build_mqtt_client_id(device_id)
    log_startup.info("Connecting to MQTT broker...")
    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=mqtt_client_id)
    mqtt_client.user_data_set({})
    mqtt_client.reconnect_delay_set(min_delay=1, max_delay=30)
    # Cap outbound queue to prevent unbounded memory growth under backpressure.
    mqtt_client.max_queued_messages_set(1000)

    if cfg.mqtt_username and cfg.mqtt_password:
        mqtt_client.username_pw_set(cfg.mqtt_username, cfg.mqtt_password)
    else:
        log_mqtt.warning(
            "MQTT broker connected without credentials — ensure broker ACLs "
            "restrict write access to nibe/ and homeassistant/ topics. "
            "Set mqtt_username and mqtt_password in the add-on options."
        )

    _configure_mqtt_tls(mqtt_client, cfg)
    mqtt_client.will_set(MGMT_AVAIL_TOPIC, "offline", retain=True)

    # _em holds entity_manager once it is built (after this function returns)
    # so the on_connect callback can call resubscribe_all / republish_availability
    # on reconnection without needing a forward reference.
    _em:           list          = []
    _auth_failed:  threading.Event = threading.Event()
    shutting_down: list[bool]    = [False]   # returned to caller
    _FATAL_RC = {4, 5}
    keepalive = _keepalive_from_config(cfg.poll_interval)

    def on_connect(_client, _userdata, _flags, reason_code, _properties):
        rc_value = reason_code.value if hasattr(reason_code, 'value') else int(reason_code)
        if rc_value == 0:
            log_mqtt.info(
                "MQTT connected to %s:%d (keepalive %ds)",
                cfg.mqtt_broker, cfg.mqtt_port, keepalive,
            )
            if _em:
                _em[0].resubscribe_all()
                _em[0].republish_availability()
        elif rc_value in _FATAL_RC:
            log_mqtt.error(
                "MQTT broker %s:%d refused the connection (reason %d) — "
                "check mqtt_username and mqtt_password in the add-on options.",
                cfg.mqtt_broker, cfg.mqtt_port, rc_value,
            )
            _auth_failed.set()
        else:
            log_mqtt.error(
                "MQTT connection to %s:%d failed: %s",
                cfg.mqtt_broker, cfg.mqtt_port, reason_code,
            )

    def on_disconnect(_client, _userdata, _disconnect_flags, reason_code, _properties):
        if shutting_down[0]:
            return
        rc_value = reason_code.value if hasattr(reason_code, 'value') else int(reason_code)
        _DISCONNECT_LABELS = {
            0: "clean disconnect or connection lost",
            1: "connection refused — wrong protocol version",
            2: "connection refused — client ID rejected",
            3: "connection refused — broker unavailable",
            4: "connection refused — wrong credentials",
            5: "connection refused — not authorised",
        }
        label = _DISCONNECT_LABELS.get(rc_value, str(reason_code))
        log_mqtt.warning(
            "MQTT disconnected from %s:%d (%s) — paho will reconnect automatically",
            cfg.mqtt_broker, cfg.mqtt_port, label,
        )

    mqtt_client.on_connect    = on_connect
    mqtt_client.on_disconnect = on_disconnect

    try:
        mqtt_client.connect(cfg.mqtt_broker, cfg.mqtt_port, keepalive=keepalive)
        mqtt_client.loop_start()
        time.sleep(2)

        if _auth_failed.is_set():
            mqtt_client.loop_stop()
            sys.exit(1)

        if not mqtt_client.is_connected():
            log_mqtt.warning("MQTT not yet connected — broker may be slow, continuing anyway")
        else:
            log_mqtt.info("MQTT client connection verified")

        mqtt_client.publish(MGMT_AVAIL_TOPIC, "online", retain=True)
        log_mqtt.info("Availability topic pre-cleared to 'online'")

    except Exception as e:
        log_mqtt.error(
            "Cannot connect to MQTT broker at %s:%d — %s. "
            "Check that the broker is running and that 'mqtt_host' and 'mqtt_port' "
            "are correctly set in the add-on configuration. "
            "If using the Mosquitto add-on, the default host is 'core-mosquitto'.",
            cfg.mqtt_broker, cfg.mqtt_port, e,
        )
        sys.exit(1)

    # Returns a callable so the caller can wire entity_manager into the
    # on_connect callback after construction, without attaching anything
    # to the paho client object.
    def _set_entity_manager(em) -> None:
        _em.append(em)

    return api_client, mqtt_client, response, device_id, shutting_down, _set_entity_manager


# ============================================================================
# STARTUP SEQUENCE — assemble subsystems, restore state, start threads
# ============================================================================

def _run_startup_sequence(
    cfg:                  BridgeConfig,
    api_client,
    mqtt_client,
    response:             dict,
    device_id:            str,
    initial_mode:         str,
    log_level:            str,
    set_entity_manager,
) -> tuple:
    """Assemble all subsystems and bring the bridge to ready state.

    Steps (in order):
      1. Build EntityManager and MqttDiscoveryPublisher.
      2. Load menu structure and populate MODES['menus'].
      3. Discover points from the Nibe API.
      4. Publish management-interface entities.
      5. Scan MQTT for retained discovery configs; decide and execute startup action.
      6. Start HAEntityRegistryWatcher and Lovelace provisioning threads.
      7. Publish initial stats and device modes.

    Returns
    -------
    tuple of (entity_manager, publisher, registry_watcher, mgmt_executor)
    """
    device_info = _build_device_info(response, device_id, cfg.device_name, cfg.api_base_url)

    publisher = MqttDiscoveryPublisher(
        mqtt_client=mqtt_client,
        device_info=device_info,
        device_id=device_id,
        device_name=cfg.device_name,
    )

    entity_manager = EntityManager(
        api_client=api_client,
        publisher=publisher,
        notify_fn=notify_ha,
        dismiss_fn=dismiss_ha,
        mqtt_client=mqtt_client,
    )

    entity_manager.bulk_interval            = cfg.poll_interval
    entity_manager.api_failure_threshold    = cfg.api_failure_threshold
    entity_manager.changelog_retention_days = cfg.changelog_retention_days
    entity_manager.device_info              = device_info

    # Wire entity_manager into the on_connect reconnection callback.
    set_entity_manager(entity_manager)

    # Build point → menu reverse lookup and populate MODES['menus'] from YAML.
    # Done eagerly before the Lovelace thread starts so point_to_menu_map is
    # available for dynamic-change notifications from the first poll cycle.
    _app_dir = os.path.dirname(__file__)
    entity_manager.point_to_menu_map, MODES['menus'] = _load_menu_structure(
        _app_dir, log_if_mode=(initial_mode == 'menus')
    )

    log_startup.debug(
        "Device info: model=%s, serial=%s, firmware=%s",
        device_info.get("model"),
        device_info.get("serial_number"),
        device_info.get("model_id"),
    )

    dismiss_ha(mqtt_client, "nibe_write_error")

    # ── Discover points ───────────────────────────────────────────────────────
    if not entity_manager.discover_points():
        log_startup.warning(
            "Initial point discovery failed — device unreachable. "
            "The bridge will keep retrying in the polling loop."
        )
        notify_ha(
            mqtt_client,
            title="Nibe Bridge: Started Without Device",
            message=(
                f"The {cfg.device_name} was unreachable at startup so no entities "
                "could be loaded. The bridge is running and will restore all "
                "entities automatically when the device comes back online."
            ),
            notification_id="nibe_discovery_incomplete",
        )
        entity_manager._discovery_notification_active = True
        time.sleep(1)

    # ── Management interface ──────────────────────────────────────────────────
    publisher.publish_management_discovery(
        initial_mode, debug_mode=(log_level.lower() == 'debug')
    )
    publisher.publish_initial_device_modes(response)

    # Reset stale test result attrs from previous run so the sensor
    # attributes show a clean state after a rebuild.  The state topic is
    # intentionally not published here — doing so would trigger HA
    # automations on every restart.
    if log_level.lower() == 'debug':
        import json as _json
        mqtt_client.publish(MgmtTopic.RUN_TESTS_ATTRS, _json.dumps({
            "status": "ready",
            "summary": "No test run since last restart.",
        }), retain=True)

    mgmt_executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=2, thread_name_prefix="nibe_mgmt"
    )
    ManagementCommandHandler(mqtt_client, entity_manager, publisher, mgmt_executor).register_all()
    entity_manager._mgmt_avail_topic = MGMT_AVAIL_TOPIC

    # ── Scan / restore / apply mode ──────────────────────────────────────────
    mqtt_enabled_points = _run_scan_with_retry(entity_manager)

    applied_mode   = entity_manager.read_applied_mode() if mqtt_enabled_points else None
    startup_action = decide_startup_action(
        has_existing_entities=bool(mqtt_enabled_points),
        applied_mode=applied_mode,
        config_mode=initial_mode,
    )
    _execute_startup_action(
        entity_manager, startup_action, applied_mode, initial_mode,
        mqtt_client, cfg.device_name,
    )

    entity_manager.publish_enabled_state()
    entity_manager.publish_snapshots()
    publisher.mqtt.publish(
        MgmtTopic.STATS_STATE,
        str(len(entity_manager.mqtt_enabled_points)),
        retain=True,
    )

    # ── HA Entity Registry Watcher + Lovelace ────────────────────────────────
    registry_watcher = HAEntityRegistryWatcher(entity_manager, publisher)
    registry_watcher.start()

    lovelace_thread = threading.Thread(
        target=provision_lovelace_ui,
        args=(BRIDGE_VERSION, cfg.device_name, registry_watcher,
              log_level.lower() == 'debug'),
        kwargs={"mode": initial_mode},
        name="nibe_lovelace_setup",
        daemon=True,
    )
    lovelace_thread.start()

    if initial_mode == "menus":
        schedule_menu_dashboard_regen(
            entity_manager, registry_watcher, log_level.lower() == 'debug',
            lovelace_thread=lovelace_thread,
        )
    else:
        remove_menu_dashboard()

    # ── Initial stats ─────────────────────────────────────────────────────────
    update_stats_and_health(entity_manager, publisher)
    update_device_modes(entity_manager, publisher)

    log_startup.info(
        "Bridge ready — %d points, %d enabled, %d active | poll=%ds alarm=%ds",
        len(entity_manager.all_points),
        len(entity_manager.mqtt_enabled_points),
        len(entity_manager.active_entities),
        entity_manager.bulk_interval,
        _ALARM_POLL_INTERVAL,
    )

    return entity_manager, publisher, registry_watcher, mgmt_executor


# ============================================================================
# POLL LOOP — runs until KeyboardInterrupt (SIGTERM/SIGHUP via signal handler)
# ============================================================================

def _poll_loop(
    entity_manager,
    publisher,
    initial_mode: str,
) -> None:
    """Run the main polling loop until KeyboardInterrupt is raised.

    Fires update_all_states() every bulk_interval seconds (or
    _post_write_interval during post-write scan windows) and
    update_alarm_state() every _ALARM_POLL_INTERVAL seconds.

    Exceptions from individual poll cycles are caught, logged, and
    backed off exponentially — the loop never exits on its own.
    Raises KeyboardInterrupt to signal the caller to begin shutdown.
    """
    last_update             = 0.0
    last_alarm_check        = 0.0
    last_memory_log         = 0.0
    _loop_consecutive_errors = 0

    while True:
        try:
            current_time    = time.time()
            effective_outer = (
                entity_manager._post_write_interval
                if entity_manager._post_write_active
                else entity_manager.bulk_interval
            )

            if current_time - last_update >= effective_outer:
                log_entities.debug("Periodic state update")

                deferred_ran = False
                if (not entity_manager.initial_discovery_complete
                        and entity_manager.api_consecutive_failures == 0):
                    deferred_ran = entity_manager.complete_deferred_discovery(initial_mode)

                if deferred_ran:
                    # complete_deferred_discovery already fetched bulk data —
                    # skip update_all_states to avoid a redundant API call.
                    entity_manager.last_bulk_fetch = time.time()
                else:
                    entity_manager.update_all_states()

                update_stats_and_health(entity_manager, publisher)
                update_device_modes(entity_manager, publisher)
                entity_manager._check_memory_and_cleanup()

                last_update              = current_time
                _loop_consecutive_errors = 0   # reset on a clean cycle

                # Log memory usage periodically (every 10 minutes).
                if current_time - last_memory_log >= 600:
                    try:
                        memory_stats = entity_manager.get_memory_usage()
                        log_startup.debug(
                            "Memory usage: %d points, %d active entities, ~%.2f MB "
                            "(cache sizes: value=%d, states=%d, strings=%d)",
                            memory_stats.get('total_points', 0),
                            memory_stats.get('active_entities', 0),
                            memory_stats.get('estimated_memory_mb', 0),
                            memory_stats.get('value_cache_size', 0),
                            memory_stats.get('last_states_size', 0),
                            memory_stats.get('point_string_cache_size', 0),
                        )
                    except Exception as e:
                        log_startup.error("Memory logging error: %s", e)
                    last_memory_log = current_time

            if current_time - last_alarm_check >= _ALARM_POLL_INTERVAL:
                update_alarm_state(entity_manager, publisher)
                last_alarm_check = current_time

            time.sleep(1)

        except KeyboardInterrupt:
            raise
        except Exception as e:
            _loop_consecutive_errors += 1
            backoff = min(5 * _loop_consecutive_errors, 60)
            log_startup.error(
                "Unexpected error in main loop (occurrence %d, backing off %ds): %s",
                _loop_consecutive_errors, backoff, e,
                exc_info=True,
            )
            if _loop_consecutive_errors >= 5:
                try:
                    publisher.publish_bridge_alert(
                        alert_type = "main_loop_error",
                        severity   = "error",
                        message    = (
                            f"Bridge main loop has crashed {_loop_consecutive_errors} "
                            f"times consecutively. Last error: {e}"
                        ),
                        context    = {
                            "consecutive_errors": _loop_consecutive_errors,
                            "error":              str(e),
                        },
                    )
                except Exception:
                    pass
            time.sleep(backoff)


# ============================================================================
# SHUTDOWN — drain executors, publish offline, disconnect cleanly
# ============================================================================

def _shutdown(
    entity_manager,
    publisher,
    mqtt_client,
    registry_watcher,
    mgmt_executor,
    shutting_down:    list[bool],
    atexit_cleanup_fn,
) -> None:
    """Execute a clean shutdown sequence.

    Steps (in order):
      1. Stop the HA registry watcher thread.
      2. Drain the write and management executors with a timeout.
      3. Publish 'offline' to every active entity's availability topic.
      4. Optionally wipe all retained MQTT messages (NIBE_REMOVE_FRONTEND=1).
      5. Tear down Lovelace resources.
      6. Disconnect MQTT cleanly.
    """
    log_startup.info("Shutting down...")
    registry_watcher.stop()

    log_startup.info("Waiting for in-flight commands to complete...")
    for executor, name in [
        (entity_manager._write_executor, "write"),
        (mgmt_executor,                  "management"),
    ]:
        t = threading.Thread(
            target=executor.shutdown, kwargs={"wait": True, "cancel_futures": False}
        )
        t.start()
        t.join(timeout=_SHUTDOWN_TIMEOUT)
        if t.is_alive():
            log_startup.warning(
                "%s executor did not finish within %ds — proceeding with shutdown",
                name, _SHUTDOWN_TIMEOUT,
            )

    log_startup.info("Publishing offline availability...")
    pending_publishes = []
    with entity_manager._active_entities_lock:
        for entity_info in entity_manager.active_entities:
            avail_topic = entity_info.get('availability_topic')
            if avail_topic:
                result = mqtt_client.publish(avail_topic, "offline", retain=True)
                pending_publishes.append(result)
    pending_publishes.append(
        mqtt_client.publish(MGMT_AVAIL_TOPIC, "offline", retain=True)
    )

    for pub in pending_publishes:
        try:
            pub.wait_for_publish(timeout=2.0)
        except Exception as e:
            log_mqtt.warning("Offline publish did not confirm: %s", e)

    if os.environ.get('NIBE_REMOVE_FRONTEND') == '1':
        _cleanup_mqtt_retained(mqtt_client)
    else:
        log_startup.info("MQTT discovery configs retained for next startup")

    teardown_lovelace()

    # Unregister atexit so loop_stop/disconnect are not called a second time.
    atexit.unregister(atexit_cleanup_fn)
    shutting_down[0] = True
    mqtt_client.loop_stop()
    mqtt_client.disconnect()
    log_startup.info("Service stopped cleanly")


# ============================================================================
# MAIN — thin orchestrator: parse, configure, call the four phases
# ============================================================================

def main():
    """Initialise all subsystems and run the polling loop.

    Startup sequence:
      1.  Parse CLI args and load config from all sources.
      2.  Build infrastructure: API client, MQTT client, initial connection.
      3.  Run startup sequence: assemble subsystems, restore state, start threads.
      4.  Enter the polling loop (exits only via KeyboardInterrupt / signal).
      5.  On shutdown: drain executors, publish offline, disconnect.
    """
    args      = parse_arguments()
    cfg       = load_config(cli_args=args)
    log_level = args.log_level if args.log_level else cfg.log_level
    _build_logging(level=log_level)

    for warning in cfg.warnings:
        log_api.warning("%s", warning)

    log_startup.info("Log level: %s", log_level)

    initial_mode = _resolve_initial_mode(args, cfg)
    log_startup.info("Initial mode: %s", initial_mode)

    # ── Phase 1: infrastructure ───────────────────────────────────────────────
    api_client, mqtt_client, response, device_id, shutting_down, set_entity_manager = \
        _build_infrastructure(cfg)

    # ── Atexit guard: best-effort cleanup if we crash before clean shutdown ───
    def _atexit_cleanup():
        try:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
        except Exception:
            pass

    atexit.register(_atexit_cleanup)

    # ── Phase 2: startup sequence ─────────────────────────────────────────────
    entity_manager, publisher, registry_watcher, mgmt_executor = \
        _run_startup_sequence(
            cfg, api_client, mqtt_client, response, device_id,
            initial_mode, log_level, set_entity_manager,
        )

    # ── Signal handlers: convert SIGTERM/SIGHUP into KeyboardInterrupt ────────
    # The add-on runs as a supervised container — no terminal, no Ctrl-C.
    # KeyboardInterrupt is used purely as a lightweight internal shutdown signal.
    def _sigterm_handler(signum, _frame):
        sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGHUP"
        log_startup.info("%s received — shutting down cleanly...", sig_name)
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGHUP,  _sigterm_handler)

    # ── Phase 3: poll loop ────────────────────────────────────────────────────
    try:
        _poll_loop(entity_manager, publisher, initial_mode)
    except KeyboardInterrupt:
        pass

    # ── Phase 4: shutdown ─────────────────────────────────────────────────────
    _shutdown(
        entity_manager, publisher, mqtt_client,
        registry_watcher, mgmt_executor,
        shutting_down, _atexit_cleanup,
    )


if __name__ == "__main__":
    main()
