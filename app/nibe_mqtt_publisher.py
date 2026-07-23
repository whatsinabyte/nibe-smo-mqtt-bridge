"""
nibe_mqtt_publisher.py
======================
MqttDiscoveryPublisher — builds HA MQTT discovery configs and publishes them.

Responsibilities
----------------
- All MQTT topic string construction (single source of truth).
- Building and publishing HA discovery config payloads for every entity type.
- Publishing per-entity static attributes (point ID, Modbus register, etc.).
- Publishing per-point browser metadata for the frontend card.
- Publishing management-interface discovery configs.
- Publishing state updates (stats, alarm, device mode, uptime, API health).

What this module does NOT do
-----------------------------
- No HTTP calls to the Nibe API.
- No entity lifecycle management (enable/disable tracking).
- No threading or concurrency primitives.
- No knowledge of the polling loop or startup sequencing.

Public surface
--------------
MqttDiscoveryPublisher(mqtt_client, device_info, device_id, device_name)
    .publish_entity_discovery(point, bulk_data, range_warnings_issued) → entity_info | None
    .publish_point_metadata(point)
    .publish_all_metadata(points)
    .publish_enabled_state(mqtt_enabled_points)
    Topic helpers (module-level):
        t_config, t_state, t_command, t_available, t_attributes, t_press
"""

import hashlib
import json
import logging
import time
from enum import Enum

from nibe_utils import fmt_ts as _fmt_ts
from nibe_entity_detection import (
    DEVICE_CLASS_OVERRIDES,
    UNIT_OVERRIDES,
    apply_divisor,
    clean_unit,
    create_entity_id,
    get_entity_options,
    get_value_mapping,
    map_device_class,
)

log_mqtt     = logging.getLogger("nibe.mqtt")
log_entities = logging.getLogger("nibe.entities")

# ── Topic prefix constants ─────────────────────────────────────────────────────
_HA_BASE    = "homeassistant"
MQTT_PREFIX = "nibe/browser"


# ============================================================================
# TOPIC ENUMS  — single source of truth for every fixed MQTT topic string
# ============================================================================

class MgmtTopic(str, Enum):
    """All fixed management-entity MQTT topics.

    Using ``str, Enum`` (StrEnum backport for Python < 3.11) means each member
    IS a plain string and can be passed directly anywhere a ``str`` is expected —
    no ``.value`` needed.  The enum prevents topic strings from drifting out of
    sync between ``publish_management_discovery()`` (where configs are published)
    and ``create_management_handlers()`` (where subscriptions are registered).

    Naming convention:
        <ENTITY_ID>_CONFIG   — retained discovery config topic
        <ENTITY_ID>_STATE    — retained state topic (read by HA)
        <ENTITY_ID>_SET      — command topic (HA → bridge)
        <ENTITY_ID>_PRESS    — button press topic (HA → bridge)
    """
    # ── Entity mode sensor (read-only — mode is config-level, restart-required) ──
    MODE_CONFIG    = f"{_HA_BASE}/sensor/nibe_active_mode/config"
    MODE_STATE     = f"{_HA_BASE}/sensor/nibe_active_mode/state"

    # ── Stats sensor ──────────────────────────────────────────────────────
    STATS_CONFIG   = f"{_HA_BASE}/sensor/nibe_entity_stats/config"
    STATS_STATE    = f"{_HA_BASE}/sensor/nibe_entity_stats/state"
    STATS_ATTRS    = f"{_HA_BASE}/sensor/nibe_entity_stats/attributes"

    # ── Aid mode switch ───────────────────────────────────────────────────
    AID_CONFIG     = f"{_HA_BASE}/switch/nibe_aid_mode/config"
    AID_STATE      = f"{_HA_BASE}/switch/nibe_aid_mode/state"
    AID_SET        = f"{_HA_BASE}/switch/nibe_aid_mode/set"

    # ── Smart mode select ─────────────────────────────────────────────────
    SMART_CONFIG   = f"{_HA_BASE}/select/nibe_smart_mode/config"
    SMART_STATE    = f"{_HA_BASE}/select/nibe_smart_mode/state"
    SMART_SET      = f"{_HA_BASE}/select/nibe_smart_mode/set"

    # ── Active alarms sensor ──────────────────────────────────────────────
    ALARM_CONFIG   = f"{_HA_BASE}/sensor/nibe_notifications/config"
    ALARM_STATE    = f"{_HA_BASE}/sensor/nibe_notifications/state"
    ALARM_ATTRS    = f"{_HA_BASE}/sensor/nibe_notifications/attributes"

    # ── Reset alarms button ───────────────────────────────────────────────
    ALARM_RESET_CONFIG = f"{_HA_BASE}/button/nibe_reset_alarms/config"
    ALARM_RESET_PRESS  = f"{_HA_BASE}/button/nibe_reset_alarms/press"

    # ── Force poll button ─────────────────────────────────────────────────
    FORCE_POLL_CONFIG  = f"{_HA_BASE}/button/nibe_force_poll/config"
    FORCE_POLL_PRESS   = f"{_HA_BASE}/button/nibe_force_poll/press"

    # ── Regenerate dashboard button ───────────────────────────────────────
    REGEN_DASH_CONFIG  = f"{_HA_BASE}/button/nibe_regen_dashboard/config"
    REGEN_DASH_PRESS   = f"{_HA_BASE}/button/nibe_regen_dashboard/press"


    # ── Bridge uptime sensor ──────────────────────────────────────────────
    UPTIME_CONFIG  = f"{_HA_BASE}/sensor/nibe_bridge_uptime/config"
    UPTIME_STATE   = f"{_HA_BASE}/sensor/nibe_bridge_uptime/state"
    UPTIME_ATTRS   = f"{_HA_BASE}/sensor/nibe_bridge_uptime/attributes"

    # ── API last-fetch timestamp sensor ───────────────────────────────────
    LAST_FETCH_CONFIG = f"{_HA_BASE}/sensor/nibe_last_fetch_timestamp/config"
    LAST_FETCH_STATE  = f"{_HA_BASE}/sensor/nibe_last_fetch_timestamp/state"

    # ── API fetch duration sensor ─────────────────────────────────────────
    FETCH_DUR_CONFIG  = f"{_HA_BASE}/sensor/nibe_fetch_duration/config"
    FETCH_DUR_STATE   = f"{_HA_BASE}/sensor/nibe_fetch_duration/state"

    # ── API reachable binary_sensor ───────────────────────────────────────
    API_OK_CONFIG  = f"{_HA_BASE}/binary_sensor/nibe_api_reachable/config"
    API_OK_STATE   = f"{_HA_BASE}/binary_sensor/nibe_api_reachable/state"

    # ── Bridge availability (shared LWT / online topic) ───────────────────
    AVAIL          = f"{_HA_BASE}/sensor/nibe_bridge/available"

    # ── Enable / disable entity text inputs ──────────────────────────────
    ENABLE_SET     = f"{_HA_BASE}/text/nibe_enable_entity/set"
    DISABLE_SET    = f"{_HA_BASE}/text/nibe_disable_entity/set"

    # ── Changelog mark-read button ────────────────────────────────────────
    CHANGELOG_READ_PRESS = f"{_HA_BASE}/button/nibe_mark_changes_read/press"

    # ── Dynamic map flush button (debug only) ─────────────────────────────
    FLUSH_MAP_CONFIG = f"{_HA_BASE}/button/nibe_flush_dynamic_map/config"
    FLUSH_MAP_PRESS  = f"{_HA_BASE}/button/nibe_flush_dynamic_map/press"

    # ── Test suite runner button (debug only) ──────────────────────────────
    RUN_TESTS_CONFIG  = f"{_HA_BASE}/button/nibe_run_tests/config"
    RUN_TESTS_PRESS   = f"{_HA_BASE}/button/nibe_run_tests/press"
    RUN_TESTS_STATE   = "nibe/browser/test_suite/state"
    RUN_TESTS_ATTRS   = "nibe/browser/test_suite/attrs"


class BrowserTopic(str, Enum):
    """All fixed ``nibe/browser/`` internal MQTT topics.

    These topics are used by the frontend card and internal bridge state;
    they are not HA MQTT discovery topics.
    """
    META_TEMPLATE      = f"{MQTT_PREFIX}/meta/{{id}}"   # format with point id
    ALL_METADATA       = f"{MQTT_PREFIX}/all_metadata"   # batched: all points in one retained message
    ENABLED_STATE      = f"{MQTT_PREFIX}/enabled_state"
    DYNAMIC            = f"{MQTT_PREFIX}/dynamic"
    SCAN_SENTINEL      = f"{MQTT_PREFIX}/scan_sentinel"
    KNOWN_DYNAMIC      = f"{MQTT_PREFIX}/known_dynamic_points"   # legacy — retained for migration
    DYNAMIC_MAP        = f"{MQTT_PREFIX}/dynamic_point_map"      # DynamicPointMap table (compressed)
    ACTIVE_DYNAMIC     = f"{MQTT_PREFIX}/active_dynamic_points"  # currently active dynamic point_ids
    APPLIED_MODE       = f"{MQTT_PREFIX}/applied_mode"           # last-applied entity mode (plain string)
    DEVICE_INFO        = f"{MQTT_PREFIX}/device_info"
    POINT_LIST         = f"{MQTT_PREFIX}/point_list"
    CHANGELOG_HISTORY  = f"{MQTT_PREFIX}/changelog/history"
    CHANGELOG_UNREAD   = f"{MQTT_PREFIX}/changelog/unread"
    SNAPSHOTS          = f"{MQTT_PREFIX}/snapshots"          # retained: list of snapshots
    SNAPSHOTS_CMD      = f"{MQTT_PREFIX}/snapshots/cmd"      # command topic (card → bridge)

    # ── Observability topics ───────────────────────────────────────────────
    # BRIDGE_ALERT: non-retained, published when an alertable condition is
    #   detected (API unreachable, write failures, active alarms).  Retained
    #   would mean a stale alert persists across bridge restarts — non-retained
    #   means automations only fire on the transition edge.
    # BRIDGE_STATUS: retained, consolidated health snapshot published on every
    #   poll cycle.  Contains everything needed to diagnose the bridge state
    #   without grepping logs.
    BRIDGE_ALERT       = f"{MQTT_PREFIX}/bridge/alert"
    BRIDGE_STATUS      = f"{MQTT_PREFIX}/bridge/status"


# MGMT_AVAIL_TOPIC is imported by generate_nibe_mqtt.py.
MGMT_AVAIL_TOPIC = MgmtTopic.AVAIL

# ── Retired topics (entity-mode refactor) ──────────────────────────────────
# These belonged to the removed live preset selector (MgmtTopic.PRESET_*
# before this refactor). Kept as plain literals — not enum members — since
# they no longer exist as a live topic family; this list exists solely so
# publish_management_discovery() can clear any retained message left on the
# broker from a pre-refactor install. Publishing an empty retained payload
# is the standard MQTT mechanism for deleting a retained message; doing so
# on every startup is idempotent and cheap once the broker is clean.
_LEGACY_PRESET_TOPICS = (
    f"{_HA_BASE}/select/nibe_entity_preset/config",
    f"{_HA_BASE}/select/nibe_entity_preset/state",
    # Learning mode switch removed — DynamicPointMap learning is now always
    # active (permanently on). These clear the retained discovery config and
    # state from any install that had the switch entity.
    f"{_HA_BASE}/switch/nibe_learning_mode/config",
    f"{_HA_BASE}/switch/nibe_learning_mode/state",
)



# ============================================================================
# TOPIC BUILDERS
# ============================================================================

def t_config(entity_type: str, entity_id: str) -> str:
    return f"{_HA_BASE}/{entity_type}/{entity_id}/config"

def t_state(entity_type: str, entity_id: str) -> str:
    return f"{_HA_BASE}/{entity_type}/{entity_id}/state"

def t_command(entity_type: str, entity_id: str) -> str:
    return f"{_HA_BASE}/{entity_type}/{entity_id}/set"

def t_available(entity_type: str, entity_id: str) -> str:
    return f"{_HA_BASE}/{entity_type}/{entity_id}/available"

def t_attributes(entity_type: str, entity_id: str) -> str:
    return f"{_HA_BASE}/{entity_type}/{entity_id}/attributes"

def t_press(entity_id: str) -> str:
    return f"{_HA_BASE}/button/{entity_id}/press"


def resolve_unit(
    point_id: int,
    raw_unit: str,
    title: str = "",
    warned: set[int] | None = None,
) -> tuple[str, bool]:
    """Resolve the unit actually used for a point, applying overrides and cleaning.

    This is the single source of truth for unit resolution — both the real
    HA discovery config (publish_entity_discovery) and the Entity Manager
    card's metadata payload (publish_point_metadata / _build_point_metadata_dict)
    must call this rather than each deriving the unit independently. Two
    previously-separate code paths drifted apart this way once already (the
    card's details modal was showing the raw, pre-override, uncleaned unit
    while the real entity correctly showed the overridden/cleaned one).

    Returns (resolved_unit, was_overridden) — the second value lets callers
    (specifically the card) show the user when a value differs from what
    the firmware itself reported, rather than silently hiding the override.

    If ``warned`` is given, logs one WARNING the first time an override
    fires for a given point_id (deduplicated via the shared set so calling
    this from multiple code paths for the same point — e.g. once for
    discovery, once for the card's metadata — only logs once). Passing no
    ``warned`` set (the default) skips logging entirely, keeping pure
    resolution callers (such as direct tests) free of side effects.
    """
    was_overridden = point_id in UNIT_OVERRIDES
    unit = UNIT_OVERRIDES.get(point_id, raw_unit)
    unit = clean_unit(unit)
    if was_overridden and warned is not None and point_id not in warned:
        log_mqtt.warning(
            "Point %d (%s): unit overridden \u2014 firmware reported %r, using %r instead.",
            point_id, title or f"Point {point_id}", raw_unit, unit,
        )  # pragma: no mutate
        warned.add(point_id)
    return unit, was_overridden



# ============================================================================
# DISCOVERY PUBLISHER
# ============================================================================

class MqttDiscoveryPublisher:
    """Builds and publishes HA MQTT discovery configs and state payloads.

    Parameters
    ----------
    mqtt_client :
        A connected paho MQTT client.
    device_info : dict
        The HA device object embedded in every discovery config payload.
    device_id : str
        The bridge's stable device identifier (e.g. ``"nibe_heatpump_001"``).
    device_name : str
        Human-readable device name (e.g. ``"Nibe SMO S40"``).
    """

    def __init__(
        self,
        mqtt_client,
        device_info: dict,
        device_id: str,
        device_name: str,
    ) -> None:
        self.mqtt        = mqtt_client
        self.device_info = device_info
        self.device_id   = device_id
        self.device_name = device_name
        # Per-session set of point IDs for which a firmware range inconsistency
        # warning has already been logged.  Prevents repeat warnings every poll.
        self._range_warnings_issued: set[int] = set()
        # Per-session set of point IDs for which a unit-override warning has
        # already been logged.  Same one-shot-per-startup pattern as
        # _range_warnings_issued, kept separate so the two warning categories
        # can be reasoned about and tested independently.
        self._unit_override_warnings_issued: set[int] = set()
        # Hash of the last published discovery config per point_id.
        # Used by publish_entity_discovery to skip redundant MQTT publishes
        # when the config has not changed since the last restart.
        self._config_hashes:      dict[int, str] = {}

    # ------------------------------------------------------------------ #
    # Config hash management                                               #
    # ------------------------------------------------------------------ #

    def invalidate_config_hash(self, point_id: int) -> None:
        """Remove the cached discovery config hash for a point.

        Call this whenever a dynamic point disappears so that when it
        reappears the discovery config is unconditionally republished —
        even if the config bytes are identical to the previous publication.
        Without this, the hash-equality check in publish_entity_discovery
        suppresses the republish and HA never learns the entity is back.
        """
        self._config_hashes.pop(point_id, None)

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _pub_state(self, topic: str, payload: str) -> None:
        """Publish a retained state message and log a warning on failure.

        All management state publishers use this instead of bare mqtt.publish()
        so silent failures surface in the log rather than leaving HA sensors
        showing stale values with no indication of why.
        """
        result = self.mqtt.publish(topic, payload, retain=True)
        if result.rc != 0:
            log_mqtt.warning(
                "State publish failed for topic %s (rc=%d)", topic, result.rc
            )  # pragma: no mutate

    # ------------------------------------------------------------------ #
    # Per-entity discovery                                                 #
    # ------------------------------------------------------------------ #

    def publish_entity_discovery(
        self,
        point: dict,
        bulk_data: dict,
    ) -> dict | None:
        """Build and publish the HA MQTT discovery config for one point.

        Returns the ``entity_info`` dict on success (containing topic strings
        needed for state updates and command handling), or None if the MQTT
        publish failed.
        """
        point_id    = point['variableId']
        metadata    = point.get('metadata', {})
        entity_type = point['entity_type']
        category    = point['entity_category']
        title       = point['display_title']
        is_writable = point.get('is_writable', False)
        description = point.get('description', '')

        unit, _ = resolve_unit(point_id, metadata.get('unit', ''), title, self._unit_override_warnings_issued)

        entity_id = create_entity_id(point_id)

        config: dict = {
            "name":                  title,
            "unique_id":             f"nibe_{point_id}",
            "device":                self.device_info,
            "availability_topic":    t_available(entity_type, entity_id),
            "payload_available":     "online",
            "payload_not_available": "offline",
        }
        if category:
            config["entity_category"] = category

        if entity_type == "button":
            self._build_button_config(config, entity_id)
        elif entity_type == "switch":
            self._build_switch_config(config, entity_id)
        elif entity_type == "number":
            self._build_number_config(
                config, entity_id, point_id, title, unit, metadata, bulk_data
            )
        elif entity_type == "select":
            self._build_select_config(config, entity_id, point_id, metadata, description)
        elif entity_type == "time":
            config["state_topic"]   = t_state("time", entity_id)
            config["command_topic"] = t_command("time", entity_id)
            # Ensure no unit leaks in — time entities show HH:MM, not seconds
            config.pop("unit_of_measurement", None)
        elif entity_type == "text":
            config["state_topic"]   = t_state("text", entity_id)
            config["command_topic"] = t_command("text", entity_id)
            config["max"]           = 64   # matches Nibe string register size; also enforced server-side
        elif entity_type == "binary_sensor":
            self._build_binary_sensor_config(config, entity_id, title)
        elif entity_type == "sensor":
            self._build_sensor_config(config, entity_id, point_id, unit, title, metadata)
        else:
            # Unknown entity type — fall back to sensor so the point is still
            # visible in HA rather than silently broken.
            log_mqtt.warning(
                "Point %d: unhandled entity type %r — falling back to sensor",
                point_id, entity_type,
            )  # pragma: no mutate
            self._build_sensor_config(config, entity_id, point_id, unit, title, metadata)

        self._publish_static_attributes(
            entity_type, entity_id, point_id, unit, is_writable, description, metadata, config
        )

        config_topic   = t_config(entity_type, entity_id)
        publish_config = {k: v for k, v in config.items() if not k.startswith('_')}
        config_json    = json.dumps(publish_config, sort_keys=True)
        config_hash    = hashlib.md5(config_json.encode(), usedforsecurity=False).hexdigest()

        if self._config_hashes.get(point_id) == config_hash:
            log_mqtt.debug("Discovery config unchanged for point %d — skipping publish", point_id)  # pragma: no mutate
        else:
            log_mqtt.debug(
                "Publishing discovery for point %d (%s) as %s (category=%s)",
                point_id, title, entity_type, category,
            )  # pragma: no mutate
            result = self.mqtt.publish(config_topic, config_json, retain=True)
            if result.rc != 0:
                log_mqtt.error(
                    "Failed to publish discovery for point %d: MQTT error %d",
                    point_id, result.rc,
                )  # pragma: no mutate
                return None
            self._config_hashes[point_id] = config_hash

        return {
            'point_id':            point_id,
            'entity_id':           entity_id,
            'entity_type':         entity_type,
            'state_topic':         config.get('state_topic'),
            'command_topic':       config.get('command_topic'),
            'availability_topic':  config['availability_topic'],
            'attributes_topic':    config.get('json_attributes_topic'),
            'metadata':            metadata,
            'is_writable':         is_writable,
            'point_data':          point,
            'is_degenerate_range': config.get('_degenerate_range', False),
            # Resolved once at discovery time — avoids repeated get_value_mapping()
            # calls on every poll for select/sensor entities with enum descriptions.
            'value_mapping':       get_value_mapping(
                point_id, point, metadata.get('modbusRegisterType'),
            ),
        }

    # ------------------------------------------------------------------ #
    # Type-specific config builders (called only from publish_entity_discovery)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_button_config(config: dict, entity_id: str) -> None:
        config["command_topic"] = t_press(entity_id)

    @staticmethod
    def _build_switch_config(config: dict, entity_id: str) -> None:
        config["state_topic"]   = t_state("switch", entity_id)
        config["command_topic"] = t_command("switch", entity_id)
        config["payload_on"]    = "1"
        config["payload_off"]   = "0"
        config["optimistic"]    = False

    def _build_number_config(
        self,
        config: dict,
        entity_id: str,
        point_id: int,
        title: str,
        unit: str,
        metadata: dict,
        bulk_data: dict,
    ) -> None:
        config["state_topic"]   = t_state("number", entity_id)
        config["command_topic"] = t_command("number", entity_id)
        config["optimistic"]    = False

        min_val     = metadata.get('minValue')
        max_val     = metadata.get('maxValue')
        divisor     = metadata.get('divisor', 1) or 1
        cached      = bulk_data.get(point_id, {})
        current_raw = cached.get('raw_value')

        if min_val is not None and max_val is not None:
            unit_str = f" {unit}" if unit else ""

            if min_val == max_val:
                # Degenerate range: firmware reports min==max for this register.
                # This is detected fresh from API metadata on every entity publish
                # (including after restart) so it cannot become stale even if a
                # firmware update changes the range.  The flag bypasses write-side
                # range enforcement, which is correct: we cannot know the valid
                # range so we pass the value through and let the controller decide.
                if point_id not in self._range_warnings_issued:
                    log_entities.warning(
                        "Point %d (%s): degenerate range %g\u2013%g (min==max) "
                        "\u2014 write-side range checks bypassed.",
                        point_id, title, min_val, max_val,
                    )  # pragma: no mutate
                    self._range_warnings_issued.add(point_id)
                if current_raw is not None:
                    anchor       = current_raw / divisor
                    fallback_min = min(anchor, -100)
                    fallback_max = max(anchor,  100)
                else:
                    fallback_min = -32768 / divisor
                    fallback_max =  32767 / divisor
                config["min"]              = fallback_min
                config["max"]              = fallback_max
                config["_degenerate_range"] = True
            else:
                config["min"] = min_val / divisor
                config["max"] = max_val / divisor
                if (current_raw is not None
                        and point_id not in self._range_warnings_issued
                        and (current_raw < min_val or current_raw > max_val)):
                    log_entities.warning(
                        "Point %d (%s): current value %g%s outside firmware range "
                        "%g\u2013%g%s \u2014 writes restricted to firmware range.",
                        point_id, title,
                        current_raw / divisor, unit_str,
                        min_val / divisor, max_val / divisor, unit_str,
                    )  # pragma: no mutate
                    self._range_warnings_issued.add(point_id)
        if unit:
            config["unit_of_measurement"] = unit
        # step is the minimum increment HA allows in the number input widget.
        # It must be expressed in display units (post-divisor), so step = 1/divisor.
        # divisor=1  → step=1   (integer register: only whole numbers valid)
        # divisor=10 → step=0.1 (one decimal place register)
        # divisor=100→ step=0.01 (two decimal places)
        # Using round() with 10 decimal places avoids float representation noise
        # (e.g. 1/10 = 0.1000000000000000055… → round to 0.1).
        config["step"] = round(1 / divisor, 10)
        config["mode"] = "box"

    @staticmethod
    def _build_select_config(
        config: dict,
        entity_id: str,
        point_id: int,
        metadata: dict,
        description: str,
    ) -> None:
        config["state_topic"]   = t_state("select", entity_id)
        config["command_topic"] = t_command("select", entity_id)
        config["optimistic"]    = False
        options = get_entity_options(point_id, metadata, description)
        if options:
            config["options"] = options

    @staticmethod
    def _build_binary_sensor_config(
        config: dict, entity_id: str, title: str
    ) -> None:
        config["state_topic"] = t_state("binary_sensor", entity_id)
        config["payload_on"]  = "ON"
        config["payload_off"] = "OFF"
        device_class = map_device_class("binary_sensor", "", title)
        if device_class:
            config["device_class"] = device_class

    @staticmethod
    def _build_sensor_config(
        config: dict,
        entity_id: str,
        point_id: int,
        unit: str,
        title: str,
        metadata: dict,
    ) -> None:
        config["state_topic"] = t_state("sensor", entity_id)
        # Special case: point 2685 is a date sensor (days since 2010-01-01
        # converted to ISO date string). Set device_class and return early.
        if point_id == 2685:
            config["device_class"] = "date"
            return
        if unit:
            config["unit_of_measurement"] = unit

        _ACCUMULATING_CLASSES = {"energy", "gas", "water", "volume"}  # pragma: no mutate

        device_class = DEVICE_CLASS_OVERRIDES.get(
            point_id, map_device_class("sensor", unit, title)
        )
        is_instant = (
            point_id not in DEVICE_CLASS_OVERRIDES
            and unit == "kWh"
            and metadata.get('divisor') == 100
            and metadata.get('maxValue') == 0
            # ⚠ Heuristic: maxValue==0 is used as a proxy for "instantaneous power
            # reading" (e.g. compressor input power) rather than a lifetime energy
            # accumulator.  This works for the known Nibe register set but may
            # misclassify future firmware registers that genuinely have a zero max.
            # If a kWh sensor is wrongly treated as instantaneous, add its point_id
            # to DEVICE_CLASS_OVERRIDES in nibe_entity_detection.py to override.
        )
        has_numeric_value = bool(unit)

        if device_class in _ACCUMULATING_CLASSES and not is_instant:
            config["device_class"] = device_class
            config["state_class"]  = "total_increasing"
        elif device_class in _ACCUMULATING_CLASSES and is_instant:
            config["state_class"] = "measurement"
        elif device_class:
            config["device_class"] = device_class
            config["state_class"]  = "measurement"
        elif has_numeric_value:
            config["state_class"] = "measurement"

        # suggested_display_precision must ONLY be set for genuinely numeric
        # sensors. HA treats its mere presence as a declaration that the
        # entity is numeric, regardless of the value — setting it on a
        # string/enum status sensor (e.g. "Running", "Opening", "0.0.61")
        # causes HA to reject every state update with a ValueError, since
        # the state is text but the sensor now claims to be numeric.
        if has_numeric_value:
            decimal = metadata.get('decimal', 0)
            if decimal is not None:
                config["suggested_display_precision"] = int(decimal)

    def _publish_static_attributes(
        self,
        entity_type: str,
        entity_id: str,
        point_id: int,
        unit: str,
        is_writable: bool,
        description: str,
        metadata: dict,
        config: dict,
    ) -> None:
        """Publish static HA JSON attributes for an entity (once, retained).

        Exposes firmware metadata — point ID, Modbus register, default value,
        description, writability — as HA entity attributes.
        Wires ``json_attributes_topic`` into *config* so the discovery payload
        references the correct topic.  Skipped for button entities.
        """
        if entity_type == 'button':
            return

        attributes_topic         = t_attributes(entity_type, entity_id)
        config["json_attributes_topic"] = attributes_topic

        attr_divisor  = metadata.get('divisor', 1) or 1
        int_default   = metadata.get('intDefaultValue')
        default_with_unit = None
        if int_default is not None:
            default_display   = apply_divisor(int_default, attr_divisor)
            default_with_unit = f"{default_display} {unit}".strip()

        attributes: dict = {
            "point_id":        str(point_id),
            "modbus_register": (
                str(metadata['modbusRegisterID'])
                if metadata.get('modbusRegisterID') is not None else None
            ),
            "writable": is_writable,
        }
        if default_with_unit is not None:
            attributes["default_value"] = default_with_unit
        if description:
            attributes["description"] = description

        self.mqtt.publish(attributes_topic, json.dumps(attributes), retain=True)

    # ------------------------------------------------------------------ #
    # Frontend metadata                                                    #
    # ------------------------------------------------------------------ #

    def publish_point_metadata(self, point: dict) -> None:
        """Publish a single point's metadata to the per-point browser topic (retained).

        Called for individual point updates (e.g. after a dynamic point appears
        or disappears). For startup bulk publishing use ``publish_all_metadata``
        which sends a single batched message instead of one message per point.
        """
        point_id = point['variableId']
        metadata = self._build_point_metadata_dict(point)
        metadata["last_updated"] = time.time()
        topic = BrowserTopic.META_TEMPLATE.format(id=point_id)
        self.mqtt.publish(topic, json.dumps(metadata), retain=True)

    def _build_point_metadata_dict(self, point: dict) -> dict:
        """Return the metadata dict for a single point (shared by both publish paths).

        Unit resolution goes through resolve_unit() — the same override and
        cleaning logic used to build the real HA discovery config — so the
        Entity Manager card's details modal always reflects what was actually
        published to HA, never a stale pre-override value. ``unit_overridden``
        lets the card show the user explicitly when firmware's reported unit
        was replaced (e.g. a switch firmware mislabels with '%').
        """
        metadata_dict = point.get('metadata', {})
        point_id = point['variableId']
        unit, unit_overridden = resolve_unit(point_id, metadata_dict.get('unit', ''))
        return {
            "id":                point_id,
            "title":             point['display_title'],
            "type":              point['entity_type'],
            "writable":          point.get('is_writable', False),
            "unit":              unit,
            "unit_overridden":   unit_overridden,
            "unit_raw":          metadata_dict.get('unit', ''),
            "min_value":         metadata_dict.get('minValue'),
            "max_value":         metadata_dict.get('maxValue'),
            "category":          point.get('entity_category', ''),
            "description":       point.get('description', ''),
            "is_dynamic":        point.get('is_dynamic', False),
            "modbusRegisterID":  metadata_dict.get('modbusRegisterID'),
            "variableType":      metadata_dict.get('variableType', ''),
            "variableSize":      metadata_dict.get('variableSize', ''),
            "modbusRegisterType": metadata_dict.get('modbusRegisterType', ''),
            "shortUnit":         metadata_dict.get('shortUnit', ''),
            "divisor":           metadata_dict.get('divisor', 1),
            "decimal":           metadata_dict.get('decimal', 0),
            "change":            metadata_dict.get('change', 0),
        }

    def publish_all_metadata(self, points) -> None:
        """Publish browser metadata for all known points in a single batched message.

        Replaces the previous approach of 1063 individual per-point MQTT publishes
        with one retained ``nibe/browser/all_metadata`` message keyed by point ID.
        This reduces startup broker I/O by ~1063× for this operation (Finding 8).

        The per-point ``nibe/browser/meta/{id}`` topics are no longer published
        at bulk startup — the frontend card should subscribe to ``all_metadata``
        instead.  ``publish_point_metadata`` is still used for individual updates
        (e.g. after a dynamic point appears or disappears).
        """
        points_list = list(points)
        batch = {
            str(p['variableId']): self._build_point_metadata_dict(p)
            for p in points_list
        }
        payload = json.dumps({
            "metadata":     batch,
            "count":        len(batch),
            "last_updated": time.time(),
        })
        log_mqtt.debug("Publishing batched metadata for %d points", len(points_list))  # pragma: no mutate
        self.mqtt.publish(BrowserTopic.ALL_METADATA, payload, retain=True)

    def publish_point_list(self, all_points_by_id: dict) -> None:
        """Publish the authoritative list of all known point IDs to MQTT.

        Published retained to ``nibe/browser/point_list``.  The frontend card
        subscribes to this topic to get the ground-truth set of points —
        allowing it to detect and remove stale entries when points disappear,
        without relying on empty-payload per-point tombstones which can be
        missed if the card subscribes after the tombstone was sent.

        Called after initial discovery and after every dynamic change that
        adds or removes points.
        """
        point_ids = sorted(all_points_by_id.keys())
        payload   = json.dumps({
            "points":       point_ids,
            "count":        len(point_ids),
            "last_updated": time.time(),
        })
        self.mqtt.publish(BrowserTopic.POINT_LIST, payload, retain=True)
        log_mqtt.debug("Published point list: %d points", len(point_ids))  # pragma: no mutate


    def publish_enabled_state(self, mqtt_enabled_points: set) -> None:
        """Publish the current enabled-point list to MQTT for the frontend card."""
        enabled = list(mqtt_enabled_points)
        payload = json.dumps({
            "enabled_points": enabled,
            "count":          len(enabled),
            "timestamp":      time.time(),
        })
        log_mqtt.debug("Publishing enabled state: %d enabled points", len(enabled))  # pragma: no mutate
        self.mqtt.publish(BrowserTopic.ENABLED_STATE, payload, retain=True)

    # ------------------------------------------------------------------ #
    # State publishers — called by the poll loop                           #
    # ------------------------------------------------------------------ #

    def publish_stats(
        self,
        all_points_count:   int,
        mqtt_enabled_count: int,
        active_count:       int,
        type_counts:        dict,
        category_counts:    dict,
        writable_count:     int,
        write_total:        int = 0,
        write_success:      int = 0,
        write_failed:       int = 0,
    ) -> None:
        """Publish entity count statistics to the HA stats sensor."""
        enabled_pct = round((mqtt_enabled_count / all_points_count) * 100, 1) \
                      if all_points_count > 0 else 0
        self._pub_state(MgmtTopic.STATS_STATE, str(mqtt_enabled_count))
        self._pub_state(MgmtTopic.STATS_ATTRS, json.dumps({
            "total":              all_points_count,
            "mqtt_enabled":       mqtt_enabled_count,
            "actually_active":    active_count,
            "discrepancy":        mqtt_enabled_count - active_count,
            "enabled_percentage": enabled_pct,
            "writable_count":     writable_count,
            "by_type":            type_counts,
            "by_category":        category_counts,
            "writes_total":       write_total,
            "writes_success":     write_success,
            "writes_failed":      write_failed,
            "write_success_rate": round(write_success / write_total * 100, 1)
                                  if write_total > 0 else 100.0,
            "last_updated":       _fmt_ts(),
            "timestamp":          time.time(),
            "note":               "Counts based on MQTT retained discovery messages",
        }))

    def publish_uptime(
        self,
        bridge_start_time: float,
        api_last_success_time: float,
        api_consecutive_failures: int,
    ) -> None:
        """Publish bridge uptime and API health sensors."""
        uptime_s = int(time.time() - bridge_start_time)
        self._pub_state(MgmtTopic.UPTIME_STATE, str(uptime_s))
        self._pub_state(MgmtTopic.UPTIME_ATTRS, json.dumps({
            "started":              _fmt_ts(bridge_start_time),
            "last_api_success":     _fmt_ts(api_last_success_time),
            "consecutive_failures": api_consecutive_failures,
        }))

    def publish_api_reachability(
        self,
        api_consecutive_failures: int,
        api_failure_threshold: int,
        api_last_success_time: float,
        last_fetch_duration: float,
    ) -> None:
        """Publish API reachability binary_sensor and fetch-time sensors."""
        api_state = "OFF" if api_consecutive_failures >= api_failure_threshold else "ON"
        self._pub_state(MgmtTopic.API_OK_STATE, api_state)
        if api_last_success_time > 0:
            last_fetch_iso = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(api_last_success_time)
            )
            self._pub_state(MgmtTopic.LAST_FETCH_STATE, last_fetch_iso)
        self._pub_state(MgmtTopic.FETCH_DUR_STATE, f"{last_fetch_duration:.2f}")

    def publish_device_modes(
        self,
        aid_mode: str,
        smart_mode: str,
    ) -> None:
        """Publish aid mode switch state and smart mode select state."""
        self._pub_state(MgmtTopic.AID_STATE,   "ON" if aid_mode == "on" else "OFF")
        self._pub_state(MgmtTopic.SMART_STATE, smart_mode)

    def publish_initial_device_modes(self, device_info: dict) -> None:
        """Pre-publish AID and SMART mode initial states from device info.

        Called once at startup right after publish_management_discovery(),
        using the device info already fetched by the API client. Without this,
        the Aid Mode switch and Smart Mode select show 'Unknown' in HA for
        the entire first poll cycle (~30s at default interval).

        Uses the same state values as publish_device_modes() so HA sees a
        consistent retained state from the moment discovery configs land.
        """
        aid_on    = str(device_info.get('aidMode', 'off')).lower() == 'on'
        smart_val = str(device_info.get('smartMode', 'normal')).lower()
        self.mqtt.publish(MgmtTopic.AID_STATE,   "ON" if aid_on else "OFF", retain=True)
        self.mqtt.publish(MgmtTopic.SMART_STATE, smart_val, retain=True)
        log_mqtt.debug(
            "Pre-published initial device modes: aid=%s smart=%s",
            "ON" if aid_on else "OFF", smart_val,
        )  # pragma: no mutate

    def publish_alarm_state(
        self,
        alarm_count: int,
        clean_alarms: list,
    ) -> None:
        """Publish active alarm count and detail attributes."""
        self._pub_state(MgmtTopic.ALARM_STATE, str(alarm_count))
        self._pub_state(MgmtTopic.ALARM_ATTRS, json.dumps({
            "alarms":       clean_alarms,
            "last_updated": _fmt_ts(),
        }))

    def publish_bridge_alert(
        self,
        alert_type:  str,
        severity:    str,
        message:     str,
        context:     dict | None = None,
    ) -> None:
        """Publish a non-retained alert event to nibe/browser/bridge/alert.

        Non-retained so automations fire on the transition edge only — a
        stale retained alert would re-trigger every time HA reconnects to
        the broker.

        Parameters
        ----------
        alert_type:
            Machine-readable category: "api_unreachable", "write_failed",
            "alarm_active", "api_restored", "write_restored", "alarm_cleared".
        severity:
            "info" | "warning" | "error"
        message:
            Human-readable description suitable for an HA notification body.
        context:
            Optional dict of structured key/value pairs for additional context
            (e.g. point_id, failure_count, last_success).
        """
        payload = json.dumps({
            "alert_type":    alert_type,
            "severity":      severity,
            "message":       message,
            "timestamp":     time.time(),
            "iso_timestamp": _fmt_ts(),
            "context":       context or {},
        })
        # retain=False — alert fires on edge, not on every broker reconnect.
        log_mqtt.debug("Publishing bridge alert: type=%s, severity=%s", alert_type, severity)  # pragma: no mutate
        self.mqtt.publish(BrowserTopic.BRIDGE_ALERT, payload, retain=False)

    def publish_bridge_status(
        self,
        bridge_start_time:        float,
        api_consecutive_failures: int,
        api_failure_threshold:    int,
        api_last_success_time:    float,
        last_fetch_duration:      float,
        write_total:              int,
        write_success:            int,
        write_failed:             int,
        last_write_error:         str | None,
        pending_write_count:      int,
        mqtt_enabled_count:       int,
        all_points_count:         int,
        known_dynamic_count:      int,
    ) -> None:
        """Publish a retained consolidated health snapshot to nibe/browser/bridge/status.

        This single topic gives an external monitor or automation everything
        it needs to assess bridge health without subscribing to multiple
        individual sensor topics or grepping logs.  Retained so the current
        state is immediately available to any new subscriber.
        """
        uptime_s    = int(time.time() - bridge_start_time)
        api_healthy = api_consecutive_failures < api_failure_threshold

        payload = json.dumps({
            # Overall state
            "status":        "healthy" if api_healthy else "degraded",
            "timestamp":     time.time(),
            "iso_timestamp": _fmt_ts(),
            "uptime_s":      uptime_s,

            # API health
            "api": {
                "healthy":               api_healthy,
                "consecutive_failures":  api_consecutive_failures,
                "failure_threshold":     api_failure_threshold,
                "last_success":          _fmt_ts(api_last_success_time)
                                         if api_last_success_time > 0 else None,
                "last_fetch_duration_s": round(last_fetch_duration, 3),
            },

            # Write metrics
            "writes": {
                "total":            write_total,
                "success":          write_success,
                "failed":           write_failed,
                "pending":          pending_write_count,
                "success_rate_pct": round(write_success / write_total * 100, 1)
                                    if write_total > 0 else 100.0,
                "last_error":       last_write_error,
            },

            # Entity counts
            "entities": {
                "total_known":   all_points_count,
                "mqtt_enabled":  mqtt_enabled_count,
                "known_dynamic": known_dynamic_count,
            },
        })
        self.mqtt.publish(BrowserTopic.BRIDGE_STATUS, payload, retain=True)

    # ------------------------------------------------------------------ #
    # Management interface discovery configs                               #
    # ------------------------------------------------------------------ #

    def publish_management_discovery(self, mode: str, debug_mode: bool = False) -> None:
        """Publish HA discovery configs for all bridge management entities.

        These appear under a separate "{device_name} Management" device in HA
        and expose bridge controls rather than heat-pump data points.
        All topic strings are sourced from MgmtTopic and BrowserTopic enums —
        no literals here.

        Parameters
        ----------
        mode :
            The configured entity mode (minimal/uplink/advanced/menus/all/none).
            Only affects whether the "Regenerate Dashboard" button is
            published — it only makes sense in menus mode, where a Nibe
            Menus dashboard actually exists to regenerate.
        debug_mode :
            When True, additional debug-only entities are published — currently
            the "Flush Dynamic Map" button.  Pass True only when the bridge
            log level is DEBUG.
        """
        mgmt_device = {
            "identifiers":   [f"{self.device_id}_management"],
            "name":          f"{self.device_name} Management",
            "manufacturer":  self.device_info.get("manufacturer", "NIBE"),
            "model":         self.device_info.get("model", "Nibe S-series"),
            "serial_number": self.device_info.get("serial_number", ""),
        }
        mgmt_device = {k: v for k, v in mgmt_device.items() if v != ""}
        avail = MgmtTopic.AVAIL

        def _pub(topic, payload):
            log_mqtt.debug("Publishing management discovery for %s", topic)  # pragma: no mutate
            self.mqtt.publish(topic, json.dumps(payload), retain=True)

        # One-time (idempotent) cleanup of the pre-refactor preset selector's
        # retained messages — see _LEGACY_PRESET_TOPICS.
        for _topic in _LEGACY_PRESET_TOPICS:
            self.mqtt.publish(_topic, "", retain=True)

        _pub(MgmtTopic.MODE_CONFIG, {
            "name": "Entity Mode", "unique_id": "nibe_active_mode",
            "state_topic":   MgmtTopic.MODE_STATE,
            "availability_topic": avail,
            "device": mgmt_device, "icon": "mdi:tune", "entity_category": "diagnostic",
        })
        # Read-only diagnostic — mode is config-level and restart-required
        # (see config.yaml / en.yaml), unlike the removed live preset
        # selector. Publish the current value immediately so it isn't
        # "Unknown" until the next reconciliation; EntityManager republishes
        # this whenever the applied mode actually changes.
        self.mqtt.publish(MgmtTopic.MODE_STATE, mode, retain=True)
        _pub(MgmtTopic.STATS_CONFIG, {
            "name": f"{self.device_name} Enabled Entity Stats", "unique_id": "nibe_entity_stats",
            "state_topic": MgmtTopic.STATS_STATE, "json_attributes_topic": MgmtTopic.STATS_ATTRS,
            "availability_topic": avail, "device": mgmt_device,
            "icon": "mdi:chart-box", "entity_category": "diagnostic",
            "state_class": "measurement", "unit_of_measurement": "entities",
        })
        _pub(MgmtTopic.AID_CONFIG, {
            "name": "Aid Mode", "unique_id": "nibe_aid_mode",
            "state_topic":   MgmtTopic.AID_STATE,
            "command_topic": MgmtTopic.AID_SET,
            "availability_topic": avail,
            "payload_on": "ON", "payload_off": "OFF",
            "device": mgmt_device, "icon": "mdi:alert-circle", "entity_category": "config",
        })
        _pub(MgmtTopic.SMART_CONFIG, {
            "name": "Smart Mode", "unique_id": "nibe_smart_mode",
            "state_topic":   MgmtTopic.SMART_STATE,
            "command_topic": MgmtTopic.SMART_SET,
            "availability_topic": avail,
            "options": ["normal", "away"],
            "device": mgmt_device, "icon": "mdi:home-account", "entity_category": "config",
        })
        _pub(MgmtTopic.ALARM_CONFIG, {
            "name": f"{self.device_name} Active Alarms", "unique_id": "nibe_notifications",
            "state_topic": MgmtTopic.ALARM_STATE, "json_attributes_topic": MgmtTopic.ALARM_ATTRS,
            "availability_topic": avail, "device": mgmt_device,
            "icon": "mdi:bell-alert", "entity_category": "diagnostic",
            "state_class": "measurement", "unit_of_measurement": "alarms",
        })
        _pub(MgmtTopic.ALARM_RESET_CONFIG, {
            "name": "Reset Alarms", "unique_id": "nibe_reset_alarms",
            "command_topic": MgmtTopic.ALARM_RESET_PRESS,
            "availability_topic": avail,
            "device": mgmt_device, "icon": "mdi:bell-off", "entity_category": "config",
        })
        _pub(MgmtTopic.FORCE_POLL_CONFIG, {
            "name": "Force Poll", "unique_id": "nibe_force_poll",
            "command_topic": MgmtTopic.FORCE_POLL_PRESS,
            "availability_topic": avail,
            "device": mgmt_device, "icon": "mdi:refresh", "entity_category": "config",
        })
        if mode == "menus":
            # Only makes sense when a Nibe Menus dashboard actually exists
            # to regenerate — see publish_management_discovery docstring.
            _pub(MgmtTopic.REGEN_DASH_CONFIG, {
                "name": "Regenerate Dashboard", "unique_id": "nibe_regen_dashboard",
                "command_topic": MgmtTopic.REGEN_DASH_PRESS,
                "availability_topic": avail,
                "device": mgmt_device, "icon": "mdi:view-dashboard-edit",
                "entity_category": "config",
            })
        else:
            # Clear any retained config left over from a previous menus-mode
            # run — otherwise HA keeps showing the button as a ghost entity
            # pointing at a regen action that no longer applies.
            self.mqtt.publish(MgmtTopic.REGEN_DASH_CONFIG, "", retain=True)
        _pub(MgmtTopic.UPTIME_CONFIG, {
            "name": f"{self.device_name} Bridge Uptime", "unique_id": "nibe_bridge_uptime",
            "state_topic": MgmtTopic.UPTIME_STATE, "json_attributes_topic": MgmtTopic.UPTIME_ATTRS,
            "availability_topic": avail, "device": mgmt_device,
            "icon": "mdi:clock-outline", "entity_category": "diagnostic",
            "device_class": "duration", "unit_of_measurement": "s",
            "state_class": "total_increasing",
        })
        _pub(MgmtTopic.LAST_FETCH_CONFIG, {
            "name": "API Last Fetch", "unique_id": "nibe_last_fetch_timestamp",
            "state_topic": MgmtTopic.LAST_FETCH_STATE,
            "availability_topic": avail, "device": mgmt_device,
            "icon": "mdi:clock-check", "entity_category": "diagnostic",
            "device_class": "timestamp",
        })
        _pub(MgmtTopic.FETCH_DUR_CONFIG, {
            "name": "API Fetch Duration", "unique_id": "nibe_fetch_duration",
            "state_topic": MgmtTopic.FETCH_DUR_STATE,
            "availability_topic": avail, "device": mgmt_device,
            "icon": "mdi:timer-sand", "entity_category": "diagnostic",
            "unit_of_measurement": "s", "device_class": "duration",
            "state_class": "measurement",
        })
        _pub(MgmtTopic.API_OK_CONFIG, {
            "name": "API Reachable", "unique_id": "nibe_api_reachable",
            "state_topic": MgmtTopic.API_OK_STATE,
            "availability_topic": avail,
            "payload_on": "ON", "payload_off": "OFF",
            "device_class": "connectivity",
            "device": mgmt_device, "icon": "mdi:api", "entity_category": "diagnostic",
        })

        if debug_mode:
            _pub(MgmtTopic.FLUSH_MAP_CONFIG, {
                "name": "Flush Dynamic Map (DEBUG)", "unique_id": "nibe_flush_dynamic_map",
                "command_topic": MgmtTopic.FLUSH_MAP_PRESS,
                "availability_topic": avail,
                "device": mgmt_device, "icon": "mdi:table-refresh",
                "entity_category": "config",
            })
            _pub(MgmtTopic.RUN_TESTS_CONFIG, {
                "name": "Run Test Suite (DEBUG)", "unique_id": "nibe_run_tests",
                "command_topic": MgmtTopic.RUN_TESTS_PRESS,
                "availability_topic": avail,
                "device": mgmt_device, "icon": "mdi:test-tube",
                "entity_category": "config",
            })
            # Sensor that shows last test run result
            _pub(f"{_HA_BASE}/sensor/nibe_test_suite_result/config", {
                "name": "Test Suite Result (DEBUG)",
                "unique_id": "nibe_test_suite_result",
                "state_topic": MgmtTopic.RUN_TESTS_STATE,
                "json_attributes_topic": MgmtTopic.RUN_TESTS_ATTRS,
                "availability_topic": avail,
                "device": mgmt_device, "icon": "mdi:test-tube",
                "entity_category": "diagnostic",
            })

        # Initial sensor states
        self.mqtt.publish(MgmtTopic.UPTIME_STATE,      "0",    retain=True)
        self.mqtt.publish(MgmtTopic.API_OK_STATE,      "ON",   retain=True)
        self.mqtt.publish(MgmtTopic.FETCH_DUR_STATE,   "0.00", retain=True)
        start_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.mqtt.publish(MgmtTopic.LAST_FETCH_STATE, start_iso, retain=True)

        # Reset test suite state on every startup so a 'running' state left by
        # an interrupted run (e.g. add-on rebuild mid-test) does not persist.
        if debug_mode:
            self.mqtt.publish(MgmtTopic.RUN_TESTS_STATE, "unknown", retain=True)
            self.mqtt.publish(MgmtTopic.RUN_TESTS_ATTRS, json.dumps({
                "status":  "unknown",
                "note":    "Reset at startup — previous run may have been interrupted.",
            }), retain=True)

        # Mark management interface online
        self.mqtt.publish(MgmtTopic.AVAIL, "online", retain=True)

        # Publish device info for the frontend card
        self.mqtt.publish(BrowserTopic.DEVICE_INFO, json.dumps({
            'model':        self.device_info.get('model', 'Nibe S-series'),
            'name':         self.device_info.get('name', self.device_name),
            'manufacturer': self.device_info.get('manufacturer', 'NIBE'),
            'serial':       self.device_info.get('serial_number', ''),
        }), retain=True)
