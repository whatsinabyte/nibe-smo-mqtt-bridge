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
import threading
import time
from collections.abc import Iterable
from enum import StrEnum
from typing import Any

import nibe_discovery_config as discovery_config
from nibe_entity_detection import (
    UNIT_OVERRIDES,
    apply_divisor,
    clean_unit,
    create_entity_id,
    get_value_mapping,
)

from nibe_utils import fmt_ts as _fmt_ts

log_mqtt = logging.getLogger("nibe.mqtt")
log_entities = logging.getLogger("nibe.entities")

# ── Topic prefix constants ─────────────────────────────────────────────────────
_HA_BASE = "homeassistant"
MQTT_PREFIX = "nibe/browser"


# ============================================================================
# TOPIC ENUMS  — single source of truth for every fixed MQTT topic string
# ============================================================================


class MgmtTopic(StrEnum):
    """All fixed management-entity MQTT topics.

    Each member IS a plain string and can be passed directly anywhere a
    ``str`` is expected — no ``.value`` needed.  The enum prevents topic
    strings from drifting out of
    sync between ``publish_management_discovery()`` (where configs are published)
    and ``create_management_handlers()`` (where subscriptions are registered).

    Naming convention:
        <ENTITY_ID>_CONFIG   — retained discovery config topic
        <ENTITY_ID>_STATE    — retained state topic (read by HA)
        <ENTITY_ID>_SET      — command topic (HA → bridge)
        <ENTITY_ID>_PRESS    — button press topic (HA → bridge)
    """

    # ── Entity mode sensor (read-only — mode is config-level, restart-required) ──
    MODE_CONFIG = f"{_HA_BASE}/sensor/nibe_active_mode/config"
    MODE_STATE = f"{_HA_BASE}/sensor/nibe_active_mode/state"

    # ── Stats sensor ──────────────────────────────────────────────────────
    STATS_CONFIG = f"{_HA_BASE}/sensor/nibe_entity_stats/config"
    STATS_STATE = f"{_HA_BASE}/sensor/nibe_entity_stats/state"
    STATS_ATTRS = f"{_HA_BASE}/sensor/nibe_entity_stats/attributes"

    # ── Aid mode switch ───────────────────────────────────────────────────
    AID_CONFIG = f"{_HA_BASE}/switch/nibe_aid_mode/config"
    AID_STATE = f"{_HA_BASE}/switch/nibe_aid_mode/state"
    AID_SET = f"{_HA_BASE}/switch/nibe_aid_mode/set"

    # ── Smart mode select ─────────────────────────────────────────────────
    SMART_CONFIG = f"{_HA_BASE}/select/nibe_smart_mode/config"
    SMART_STATE = f"{_HA_BASE}/select/nibe_smart_mode/state"
    SMART_SET = f"{_HA_BASE}/select/nibe_smart_mode/set"

    # ── Active alarms sensor ──────────────────────────────────────────────
    ALARM_CONFIG = f"{_HA_BASE}/sensor/nibe_notifications/config"
    ALARM_STATE = f"{_HA_BASE}/sensor/nibe_notifications/state"
    ALARM_ATTRS = f"{_HA_BASE}/sensor/nibe_notifications/attributes"

    # ── Reset alarms button ───────────────────────────────────────────────
    ALARM_RESET_CONFIG = f"{_HA_BASE}/button/nibe_reset_alarms/config"
    ALARM_RESET_PRESS = f"{_HA_BASE}/button/nibe_reset_alarms/press"

    # ── Force poll button ─────────────────────────────────────────────────
    FORCE_POLL_CONFIG = f"{_HA_BASE}/button/nibe_force_poll/config"
    FORCE_POLL_PRESS = f"{_HA_BASE}/button/nibe_force_poll/press"

    # ── Regenerate dashboard button ───────────────────────────────────────
    REGEN_DASH_CONFIG = f"{_HA_BASE}/button/nibe_regen_dashboard/config"
    REGEN_DASH_PRESS = f"{_HA_BASE}/button/nibe_regen_dashboard/press"

    # ── Bridge uptime sensor ──────────────────────────────────────────────
    UPTIME_CONFIG = f"{_HA_BASE}/sensor/nibe_bridge_uptime/config"
    UPTIME_STATE = f"{_HA_BASE}/sensor/nibe_bridge_uptime/state"
    UPTIME_ATTRS = f"{_HA_BASE}/sensor/nibe_bridge_uptime/attributes"

    # ── API last-fetch timestamp sensor ───────────────────────────────────
    LAST_FETCH_CONFIG = f"{_HA_BASE}/sensor/nibe_last_fetch_timestamp/config"
    LAST_FETCH_STATE = f"{_HA_BASE}/sensor/nibe_last_fetch_timestamp/state"

    # ── API fetch duration sensor ─────────────────────────────────────────
    FETCH_DUR_CONFIG = f"{_HA_BASE}/sensor/nibe_fetch_duration/config"
    FETCH_DUR_STATE = f"{_HA_BASE}/sensor/nibe_fetch_duration/state"

    # ── API reachable binary_sensor ───────────────────────────────────────
    API_OK_CONFIG = f"{_HA_BASE}/binary_sensor/nibe_api_reachable/config"
    API_OK_STATE = f"{_HA_BASE}/binary_sensor/nibe_api_reachable/state"

    # ── Bridge availability (shared LWT / online topic) ───────────────────
    AVAIL = f"{_HA_BASE}/sensor/nibe_bridge/available"

    # ── Enable / disable entity text inputs ──────────────────────────────
    ENABLE_SET = f"{_HA_BASE}/text/nibe_enable_entity/set"
    DISABLE_SET = f"{_HA_BASE}/text/nibe_disable_entity/set"

    # ── Changelog mark-read button ────────────────────────────────────────
    CHANGELOG_READ_PRESS = f"{_HA_BASE}/button/nibe_mark_changes_read/press"

    # ── Dynamic map flush button (debug only) ─────────────────────────────
    FLUSH_MAP_CONFIG = f"{_HA_BASE}/button/nibe_flush_dynamic_map/config"
    FLUSH_MAP_PRESS = f"{_HA_BASE}/button/nibe_flush_dynamic_map/press"

    # ── Test suite runner button (debug only) ──────────────────────────────
    RUN_TESTS_CONFIG = f"{_HA_BASE}/button/nibe_run_tests/config"
    RUN_TESTS_PRESS = f"{_HA_BASE}/button/nibe_run_tests/press"
    RUN_TESTS_STATE = "nibe/browser/test_suite/state"
    RUN_TESTS_ATTRS = "nibe/browser/test_suite/attrs"

    # ── API connectivity check button (debug only) ─────────────────────────
    TEST_CONNECTION_CONFIG = f"{_HA_BASE}/button/nibe_test_connection/config"
    TEST_CONNECTION_PRESS = f"{_HA_BASE}/button/nibe_test_connection/press"
    TEST_CONNECTION_STATE = "nibe/browser/connectivity_check/state"
    TEST_CONNECTION_ATTRS = "nibe/browser/connectivity_check/attrs"


class BrowserTopic(StrEnum):
    """All fixed ``nibe/browser/`` internal MQTT topics.

    These topics are used by the frontend card and internal bridge state;
    they are not HA MQTT discovery topics.
    """

    META_TEMPLATE = f"{MQTT_PREFIX}/meta/{{id}}"  # format with point id
    ALL_METADATA = f"{MQTT_PREFIX}/all_metadata"  # batched: all points in one retained message
    ENABLED_STATE = f"{MQTT_PREFIX}/enabled_state"
    DYNAMIC = f"{MQTT_PREFIX}/dynamic"
    SCAN_SENTINEL = f"{MQTT_PREFIX}/scan_sentinel"
    KNOWN_DYNAMIC = f"{MQTT_PREFIX}/known_dynamic_points"  # legacy — retained for migration
    DYNAMIC_MAP = f"{MQTT_PREFIX}/dynamic_point_map"  # DynamicPointMap table (compressed)
    ACTIVE_DYNAMIC = f"{MQTT_PREFIX}/active_dynamic_points"  # currently active dynamic point_ids
    APPLIED_MODE = f"{MQTT_PREFIX}/applied_mode"  # last-applied entity mode (plain string)
    WANTED_POINTS = (
        f"{MQTT_PREFIX}/wanted_points"  # user-enabled point_ids, catch-all re-enable set
    )
    DEVICE_INFO = f"{MQTT_PREFIX}/device_info"
    POINT_LIST = f"{MQTT_PREFIX}/point_list"
    CHANGELOG_HISTORY = f"{MQTT_PREFIX}/changelog/history"
    CHANGELOG_UNREAD = f"{MQTT_PREFIX}/changelog/unread"
    SNAPSHOTS = f"{MQTT_PREFIX}/snapshots"  # retained: list of snapshots
    SNAPSHOTS_CMD = f"{MQTT_PREFIX}/snapshots/cmd"  # command topic (card → bridge)

    # ── Observability topics ───────────────────────────────────────────────
    # BRIDGE_ALERT: non-retained, published when an alertable condition is
    #   detected (API unreachable, write failures, active alarms).  Retained
    #   would mean a stale alert persists across bridge restarts — non-retained
    #   means automations only fire on the transition edge.
    # BRIDGE_STATUS: retained, consolidated health snapshot published on every
    #   poll cycle.  Contains everything needed to diagnose the bridge state
    #   without grepping logs.
    BRIDGE_ALERT = f"{MQTT_PREFIX}/bridge/alert"
    BRIDGE_STATUS = f"{MQTT_PREFIX}/bridge/status"


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
    # title's default value only ever surfaces inside the already
    # pragma'd, log-only warning call below (`title or f"Point {point_id}"`)
    # — verified empirically, no test distinguishes it.
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
        # pragma: no mutate start
        log_mqtt.warning(
            "Point %d (%s): unit overridden \u2014 firmware reported %r, using %r instead.",
            point_id,
            title or f"Point {point_id}",
            raw_unit,
            unit,
        )
        # pragma: no mutate end
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
        mqtt_client: Any,
        device_info: dict,
        device_id: str,
        device_name: str,
    ) -> None:
        self.mqtt = mqtt_client
        self.device_info = device_info
        self.device_id = device_id
        self.device_name = device_name
        # Per-session set of point IDs for which a firmware range inconsistency
        # warning has already been logged.  Prevents repeat warnings every poll.
        self._range_warnings_issued: set[int] = set()
        # Per-session set of point IDs for which a unit-override warning has
        # already been logged.  Same one-shot-per-startup pattern as
        # _range_warnings_issued, kept separate so the two warning categories
        # can be reasoned about and tested independently.
        self._unit_override_warnings_issued: set[int] = set()
        # publish_entity_discovery() can run concurrently for the same point_id
        # from more than one thread (the HA registry watcher thread and
        # mgmt_executor command workers both call enable_entity() for the same
        # point). The two sets above use a check-then-add pattern, so without
        # this lock two threads can both pass the "not yet warned" check before
        # either adds the point_id, duplicating the one-shot warning.
        self._warnings_lock = threading.Lock()
        # Hash of the last published discovery config per point_id.
        # Used by publish_entity_discovery to skip redundant MQTT publishes
        # when the config has not changed since the last restart.
        self._config_hashes: dict[int, str] = {}
        # entity_type last published per point_id — t_config() embeds
        # entity_type in the topic path, so if a point's entity_type is
        # ever re-derived to something different (e.g. metadata changes
        # its classification), the old topic must be explicitly cleared
        # or its retained discovery config would linger in HA forever as
        # a ghost/duplicate entity that nothing ever removes.
        self._point_entity_types: dict[int, str] = {}
        # Every entity_type domain seen retained on the broker for a point_id
        # at startup (see seed_entity_type_from_retained). A point can have
        # more than one stale domain retained simultaneously — e.g. right
        # after this cleanup logic itself first ships, an install may still
        # have an old binary_sensor config lingering alongside the already-
        # correct sensor one. A single "first-seen" value (previously stored
        # via setdefault on _point_entity_types) depends on the arbitrary
        # order retained messages arrive from the broker across *different*
        # topics, and so isn't reliable — this accumulates the full set
        # instead so every stale domain can be cleared regardless of arrival
        # order. Consumed (and cleared) by publish_entity_discovery.
        self._point_retained_domains: dict[int, set[str]] = {}
        # Hash of the last published static-attributes JSON per point_id.
        # Independent of _config_hashes: description/intDefaultValue feed
        # the attributes payload but are NOT part of the hashed discovery
        # config for most entity types (build_sensor_config/build_number_
        # config/etc. don't consume them), so gating the attributes
        # republish on config_hash alone would let a firmware description/
        # default-value change go unpublished indefinitely whenever nothing
        # else about the point changed.
        self._attributes_hashes: dict[int, str] = {}

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
        self._point_entity_types.pop(point_id, None)
        self._point_retained_domains.pop(point_id, None)
        self._attributes_hashes.pop(point_id, None)

    def seed_config_hash_from_retained(self, point_id: int, payload: bytes) -> None:
        """Pre-seed the dedup cache from a retained discovery config found
        on the broker at startup (see EntityManager.scan_mqtt_discovery).

        This publisher instance is reconstructed fresh on every process
        restart, so _config_hashes always starts empty — without this,
        restore_from_mqtt()'s claim of skipping unchanged configs on
        restart would never actually happen, and every point's discovery
        config would be unconditionally republished on every restart
        regardless of whether it changed. *payload* must be the exact
        retained MQTT payload bytes, since that is what was actually
        published last time (and therefore hashes identically to what a
        fresh, unchanged publish_entity_discovery() call would produce).
        """
        # usedforsecurity has no effect on hexdigest() output on a standard
        # (non-FIPS-restricted) system — see the empirically-verified
        # equivalence at the config_hash computation in publish_entity_discovery.
        payload_hash = hashlib.md5(payload, usedforsecurity=False).hexdigest()  # pragma: no mutate
        self._config_hashes[point_id] = payload_hash

    def seed_entity_type_from_retained(self, point_id: int, entity_type: str) -> None:
        """Record a retained discovery config's own domain, found on the
        broker at startup (see EntityManager.scan_mqtt_discovery), as a
        possibly-stale entity_type for this point_id.

        Without this, _point_retained_domains always starts empty on a fresh
        process, so publish_entity_discovery's entity_type-change cleanup
        can never fire on the very restart where a point's classification
        changes — the old retained config topic for the point's previous
        entity_type is left behind as an orphaned ghost entity in HA. Adds
        to a set (not a single overwritten value) because more than one
        stale domain can legitimately be retained for the same point_id at
        once — e.g. transitional state before this cleanup mechanism has had
        a chance to clear an old one — and the broker gives no ordering
        guarantee across messages on different topics, so remembering only
        the first- or last-seen domain would non-deterministically miss one.
        """
        self._point_retained_domains.setdefault(point_id, set()).add(entity_type)

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
            # pragma: no mutate start
            log_mqtt.warning("State publish failed for topic %s (rc=%d)", topic, result.rc)
            # pragma: no mutate end

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
        point_id = point["variableId"]
        metadata = point.get("metadata", {})
        entity_type = point["entity_type"]
        category = point["entity_category"]
        title = point["display_title"]
        is_writable = point.get("is_writable", False)
        description = point.get("description", "")

        with self._warnings_lock:
            # title here only ever surfaces inside resolve_unit's own
            # pragma'd, log-only warning call (title or f"Point {point_id}")
            # — same equivalence as resolve_unit's own title default,
            # verified empirically at this call site too.
            # A None/dropped default for metadata.get('unit', ...) is also
            # unobservable: clean_unit() explicitly treats any non-str
            # (including None) as '' — see its own docstring guarantee of
            # "always returns a string, never None". Only a wrong non-None
            # default (e.g. 'XXXX') is observable. Verified empirically.
            unit, _ = resolve_unit(
                point_id, metadata.get("unit", ""), title, self._unit_override_warnings_issued
            )

        entity_id = create_entity_id(point_id)

        config: dict = {
            "name": title,
            "unique_id": f"nibe_{point_id}",
            "device": self.device_info,
            "availability_topic": t_available(entity_type, entity_id),
            "payload_available": "online",
            "payload_not_available": "offline",
        }
        if category:
            config["entity_category"] = category

        if entity_type == "button":
            discovery_config.build_button_config(config, t_press(entity_id))
        elif entity_type == "switch":
            discovery_config.build_switch_config(
                config, t_state("switch", entity_id), t_command("switch", entity_id)
            )
        elif entity_type == "number":
            with self._warnings_lock:
                # title is only ever read inside build_number_config's own
                # pragma'd, log-only warning calls — never affects config
                # output. Verified empirically.
                discovery_config.build_number_config(
                    config,
                    t_state("number", entity_id),
                    t_command("number", entity_id),
                    point_id,
                    title,
                    unit,
                    metadata,
                    bulk_data,
                    self._range_warnings_issued,
                )
        elif entity_type == "select":
            discovery_config.build_select_config(
                config,
                t_state("select", entity_id),
                t_command("select", entity_id),
                point_id,
                description,
            )
        elif entity_type == "time":
            config["state_topic"] = t_state("time", entity_id)
            config["command_topic"] = t_command("time", entity_id)
            config["optimistic"] = False
            # Ensure no unit leaks in — time entities show HH:MM, not seconds.
            # Defensive: config never has this key yet at this point in the
            # function, so the .pop() key/default are currently unobservable.
            config.pop("unit_of_measurement", None)  # pragma: no mutate
        elif entity_type == "text":
            config["state_topic"] = t_state("text", entity_id)
            config["command_topic"] = t_command("text", entity_id)
            config["optimistic"] = False
            config["max"] = 64  # matches Nibe string register size; also enforced server-side
        elif entity_type == "binary_sensor":
            # title is passed through to map_device_class("binary_sensor", "", title),
            # which returns None unconditionally for entity_type=="binary_sensor"
            # via its own dedicated early-return branch, before title is ever
            # read — unobservable regardless of title's value. Verified empirically.
            discovery_config.build_binary_sensor_config(
                config, t_state("binary_sensor", entity_id), title
            )
        elif entity_type == "sensor":
            # The "sensor" comparison itself (and the string's exact casing/
            # content) is unobservable: any entity_type that doesn't match one
            # of the earlier branches falls into the `else` fallback below,
            # which calls discovery_config.build_sensor_config with the exact
            # same arguments and topic — byte-identical config output, just
            # with an extra pragma'd (untested) warning log. Verified empirically.
            discovery_config.build_sensor_config(
                config, t_state("sensor", entity_id), point_id, unit, title, metadata
            )
        else:
            # Unknown entity type — fall back to sensor so the point is still
            # visible in HA rather than silently broken.
            # pragma: no mutate start
            log_mqtt.warning(
                "Point %d: unhandled entity type %r — falling back to sensor",
                point_id,
                entity_type,
            )
            # pragma: no mutate end
            discovery_config.build_sensor_config(
                config, t_state("sensor", entity_id), point_id, unit, title, metadata
            )

        # publish=None is unobservable: _publish_static_attributes only ever
        # checks `if publish:`, where None and False are both falsy —
        # verified empirically.
        static_attributes = self._publish_static_attributes(
            entity_type,
            entity_id,
            point_id,
            unit,
            is_writable,
            description,
            metadata,
            config,
            publish=False,
        )

        config_topic = t_config(entity_type, entity_id)
        publish_config = {k: v for k, v in config.items() if not k.startswith("_")}
        config_json = json.dumps(publish_config, sort_keys=True)
        config_hash = hashlib.md5(
            config_json.encode(), usedforsecurity=False
        ).hexdigest()  # pragma: no mutate — flag has no effect on hexdigest() output

        # Union of every domain this point_id is known to have been published
        # under: this session's own last publish (_point_entity_types), plus
        # every domain seen retained on the broker at startup
        # (_point_retained_domains — can hold more than one entry; see
        # seed_entity_type_from_retained). Any of those that isn't the
        # entity_type we're about to publish now is stale and must be
        # cleared, not just a single remembered "previous" value — a point
        # can have more than one leftover domain retained simultaneously.
        stale_domains = self._point_retained_domains.pop(point_id, set())
        prev_entity_type = self._point_entity_types.get(point_id)
        if prev_entity_type is not None:
            stale_domains.add(prev_entity_type)
        stale_domains.discard(entity_type)

        if stale_domains:
            # entity_type changed since the last publish (this session or a
            # prior one) — clear every stale domain's retained discovery
            # config so HA doesn't keep showing a ghost/duplicate entity
            # that nothing else would ever remove. Sorted for deterministic
            # ordering (log output, test assertions) — the broker publish
            # order doesn't matter functionally.
            for stale_domain in sorted(stale_domains):
                old_topic = t_config(stale_domain, entity_id)
                self.mqtt.publish(old_topic, "", retain=True)
                # pragma: no mutate start
                log_mqtt.info(
                    "Point %d: entity_type changed %s -> %s — cleared old discovery topic %s",
                    point_id,
                    stale_domain,
                    entity_type,
                    old_topic,
                )
                # pragma: no mutate end
            # Force a fresh publish below even if the new config's hash
            # happens to collide with whatever was last stored.
            self._config_hashes.pop(point_id, None)

        if self._config_hashes.get(point_id) == config_hash:
            log_mqtt.debug(
                "Discovery config unchanged for point %d — skipping publish", point_id
            )  # pragma: no mutate
        else:
            # pragma: no mutate start
            log_mqtt.debug(
                "Publishing discovery for point %d (%s) as %s (category=%s)",
                point_id,
                title,
                entity_type,
                category,
            )
            # pragma: no mutate end
            result = self.mqtt.publish(config_topic, config_json, retain=True)
            if result.rc != 0:
                # pragma: no mutate start
                log_mqtt.error(
                    "Failed to publish discovery for point %d: MQTT error %d",
                    point_id,
                    result.rc,
                )
                # pragma: no mutate end
                return None
            self._config_hashes[point_id] = config_hash
            self._point_entity_types[point_id] = entity_type

        # Gated on its own hash, independent of config_hash: description and
        # intDefaultValue feed this payload but are NOT part of the hashed
        # discovery config for most entity types, so a firmware description/
        # default-value change with nothing else different about the point
        # would never republish if this were still gated on config_hash
        # alone (see _attributes_hashes' declaration in __init__).
        if static_attributes is not None:
            attributes_topic, attributes_json = static_attributes
            # usedforsecurity has no effect on hexdigest() output on a standard
            # (non-FIPS-restricted) system — see the empirically-verified
            # equivalence at the config_hash computation in publish_entity_discovery.
            # pragma: no mutate start
            attributes_hash = hashlib.md5(
                attributes_json.encode(), usedforsecurity=False
            ).hexdigest()
            # pragma: no mutate end
            if self._attributes_hashes.get(point_id) != attributes_hash:
                self.mqtt.publish(attributes_topic, attributes_json, retain=True)
                self._attributes_hashes[point_id] = attributes_hash

        return {
            "point_id": point_id,
            "entity_id": entity_id,
            "entity_type": entity_type,
            "state_topic": config.get("state_topic"),
            "command_topic": config.get("command_topic"),
            "availability_topic": config["availability_topic"],
            "attributes_topic": config.get("json_attributes_topic"),
            "metadata": metadata,
            "is_writable": is_writable,
            "point_data": point,
            "is_degenerate_range": config.get("_degenerate_range", False),
            # Resolved once at discovery time — avoids repeated get_value_mapping()
            # calls on every poll for select/sensor entities with enum descriptions.
            # The register_type argument (metadata.get('modbusRegisterType'))
            # is entirely unused inside get_value_mapping's body — any key,
            # default, or dropped-argument mutation to it is unobservable.
            # Verified empirically. point_id, in contrast, IS used (manual
            # VALUE_MAPPINGS lookup) and is covered by a dedicated test.
            "value_mapping": get_value_mapping(
                point_id,
                point,
                metadata.get("modbusRegisterType"),
            ),
        }

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
        publish: bool = True,
    ) -> tuple[str, str] | None:
        """Publish static HA JSON attributes for an entity (once, retained).

        Exposes firmware metadata — point ID, Modbus register, default value,
        description, writability — as HA entity attributes.
        Wires ``json_attributes_topic`` into *config* so the discovery payload
        references the correct topic, regardless of *publish*.  Skipped for
        button entities (returns None).

        *publish* defaults to True (publish immediately, matching every
        caller's expectation and every existing test of this method). Pass
        ``publish=False`` to only build the payload and wire ``config``
        without publishing — the caller then gets back (attributes_topic,
        attributes_json) to publish itself, conditionally (see
        publish_entity_discovery, which only republishes this retained topic
        when the discovery config itself actually changed, rather than
        rewriting an identical retained payload on every poll cycle).
        """
        if entity_type == "button":
            return None

        attributes_topic = t_attributes(entity_type, entity_id)
        config["json_attributes_topic"] = attributes_topic

        # A None/dropped default (with the correct 'divisor' key) is
        # unobservable: both fall through the trailing `or 1` to the same
        # result as the real default (1). A WRONG key, wrong default value,
        # or wrong `or` fallback IS observable whenever divisor is present
        # with a real non-1 value, is explicitly 0, or is absent entirely —
        # verified empirically.
        attr_divisor = metadata.get("divisor", 1) or 1
        int_default = metadata.get("intDefaultValue")
        default_with_unit = None
        if int_default is not None:
            default_display = apply_divisor(int_default, attr_divisor)
            default_with_unit = f"{default_display} {unit}".strip()

        attributes: dict = {
            "point_id": str(point_id),
            "modbus_register": (
                str(metadata["modbusRegisterID"])
                if metadata.get("modbusRegisterID") is not None
                else None
            ),
            "writable": is_writable,
        }
        if default_with_unit is not None:
            attributes["default_value"] = default_with_unit
        if description:
            attributes["description"] = description

        attributes_json = json.dumps(attributes)
        if publish:
            self.mqtt.publish(attributes_topic, attributes_json, retain=True)
        return attributes_topic, attributes_json

    # ------------------------------------------------------------------ #
    # Frontend metadata                                                    #
    # ------------------------------------------------------------------ #

    def publish_point_metadata(self, point: dict) -> None:
        """Publish a single point's metadata to the per-point browser topic (retained).

        Called for individual point updates (e.g. after a dynamic point appears
        or disappears). For startup bulk publishing use ``publish_all_metadata``
        which sends a single batched message instead of one message per point.
        """
        point_id = point["variableId"]
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
        metadata_dict = point.get("metadata", {})
        point_id = point["variableId"]
        # A None/dropped default for metadata_dict.get('unit', ...) is
        # unobservable: clean_unit() explicitly treats any non-str
        # (including None) as '' — same equivalence as the identical
        # pattern in publish_entity_discovery. Verified empirically.
        unit, unit_overridden = resolve_unit(point_id, metadata_dict.get("unit", ""))
        return {
            "id": point_id,
            "title": point["display_title"],
            "type": point["entity_type"],
            "writable": point.get("is_writable", False),
            "unit": unit,
            "unit_overridden": unit_overridden,
            "unit_raw": metadata_dict.get("unit", ""),
            "min_value": metadata_dict.get("minValue"),
            "max_value": metadata_dict.get("maxValue"),
            "category": point.get("entity_category", ""),
            "description": point.get("description", ""),
            "is_dynamic": point.get("is_dynamic", False),
            "modbusRegisterID": metadata_dict.get("modbusRegisterID"),
            "variableType": metadata_dict.get("variableType", ""),
            "variableSize": metadata_dict.get("variableSize", ""),
            "modbusRegisterType": metadata_dict.get("modbusRegisterType", ""),
            "shortUnit": metadata_dict.get("shortUnit", ""),
            "divisor": metadata_dict.get("divisor", 1),
            "decimal": metadata_dict.get("decimal", 0),
            "change": metadata_dict.get("change", 0),
        }

    def publish_all_metadata(self, points: Iterable[dict]) -> None:
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
        batch = {str(p["variableId"]): self._build_point_metadata_dict(p) for p in points_list}
        payload = json.dumps(
            {
                "metadata": batch,
                "count": len(batch),
                "last_updated": time.time(),
            }
        )
        log_mqtt.debug(
            "Publishing batched metadata for %d points", len(points_list)
        )  # pragma: no mutate
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
        payload = json.dumps(
            {
                "points": point_ids,
                "count": len(point_ids),
                "last_updated": time.time(),
            }
        )
        self.mqtt.publish(BrowserTopic.POINT_LIST, payload, retain=True)
        log_mqtt.debug("Published point list: %d points", len(point_ids))  # pragma: no mutate

    def publish_enabled_state(self, mqtt_enabled_points: set) -> None:
        """Publish the current enabled-point list to MQTT for the frontend card."""
        enabled = list(mqtt_enabled_points)
        payload = json.dumps(
            {
                "enabled_points": enabled,
                "count": len(enabled),
                "timestamp": time.time(),
            }
        )
        log_mqtt.debug(
            "Publishing enabled state: %d enabled points", len(enabled)
        )  # pragma: no mutate
        self.mqtt.publish(BrowserTopic.ENABLED_STATE, payload, retain=True)

    # ------------------------------------------------------------------ #
    # State publishers — called by the poll loop                           #
    # ------------------------------------------------------------------ #

    def publish_stats(
        self,
        all_points_count: int,
        mqtt_enabled_count: int,
        active_count: int,
        type_counts: dict,
        category_counts: dict,
        writable_count: int,
        write_total: int = 0,
        write_success: int = 0,
        write_failed: int = 0,
    ) -> None:
        """Publish entity count statistics to the HA stats sensor."""
        enabled_pct = (
            round((mqtt_enabled_count / all_points_count) * 100, 1) if all_points_count > 0 else 0
        )
        self._pub_state(MgmtTopic.STATS_STATE, str(mqtt_enabled_count))
        self._pub_state(
            MgmtTopic.STATS_ATTRS,
            json.dumps(
                {
                    "total": all_points_count,
                    "mqtt_enabled": mqtt_enabled_count,
                    "actually_active": active_count,
                    "discrepancy": mqtt_enabled_count - active_count,
                    "enabled_percentage": enabled_pct,
                    "writable_count": writable_count,
                    "by_type": type_counts,
                    "by_category": category_counts,
                    "writes_total": write_total,
                    "writes_success": write_success,
                    "writes_failed": write_failed,
                    "write_success_rate": round(write_success / write_total * 100, 1)
                    if write_total > 0
                    else 100.0,
                    "last_updated": _fmt_ts(),
                    "timestamp": time.time(),
                    "note": "Counts based on MQTT retained discovery messages",
                }
            ),
        )

    def publish_uptime(
        self,
        bridge_start_time: float,
        api_last_success_time: float,
        api_consecutive_failures: int,
    ) -> None:
        """Publish bridge uptime and API health sensors."""
        uptime_s = int(time.time() - bridge_start_time)
        self._pub_state(MgmtTopic.UPTIME_STATE, str(uptime_s))
        self._pub_state(
            MgmtTopic.UPTIME_ATTRS,
            json.dumps(
                {
                    "started": _fmt_ts(bridge_start_time),
                    "last_api_success": _fmt_ts(api_last_success_time),
                    "consecutive_failures": api_consecutive_failures,
                }
            ),
        )

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
            last_fetch_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(api_last_success_time))
            self._pub_state(MgmtTopic.LAST_FETCH_STATE, last_fetch_iso)
        self._pub_state(MgmtTopic.FETCH_DUR_STATE, f"{last_fetch_duration:.2f}")

    def publish_device_modes(
        self,
        aid_mode: str,
        smart_mode: str,
    ) -> None:
        """Publish aid mode switch state and smart mode select state."""
        self._pub_state(MgmtTopic.AID_STATE, "ON" if aid_mode == "on" else "OFF")
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
        # aidMode's default is only ever compared via `== 'on'` after
        # .lower() — any default that doesn't itself become 'on' (None,
        # case variants, etc.) is unobservable. smartMode's default is only
        # ever published after .lower(), which normalises case mutations
        # away. Verified empirically.
        aid_on = str(device_info.get("aidMode", "off")).lower() == "on"
        smart_val = str(device_info.get("smartMode", "normal")).lower()
        self.mqtt.publish(MgmtTopic.AID_STATE, "ON" if aid_on else "OFF", retain=True)
        self.mqtt.publish(MgmtTopic.SMART_STATE, smart_val, retain=True)
        # pragma: no mutate start
        log_mqtt.debug(
            "Pre-published initial device modes: aid=%s smart=%s",
            "ON" if aid_on else "OFF",
            smart_val,
        )
        # pragma: no mutate end

    def publish_alarm_state(
        self,
        alarm_count: int,
        clean_alarms: list,
    ) -> None:
        """Publish active alarm count and detail attributes."""
        self._pub_state(MgmtTopic.ALARM_STATE, str(alarm_count))
        self._pub_state(
            MgmtTopic.ALARM_ATTRS,
            json.dumps(
                {
                    "alarms": clean_alarms,
                    "last_updated": _fmt_ts(),
                }
            ),
        )

    def publish_bridge_alert(
        self,
        alert_type: str,
        severity: str,
        message: str,
        context: dict | None = None,
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
        payload = json.dumps(
            {
                "alert_type": alert_type,
                "severity": severity,
                "message": message,
                "timestamp": time.time(),
                "iso_timestamp": _fmt_ts(),
                "context": context or {},
            }
        )
        # retain=False — alert fires on edge, not on every broker reconnect.
        log_mqtt.debug(
            "Publishing bridge alert: type=%s, severity=%s", alert_type, severity
        )  # pragma: no mutate
        self.mqtt.publish(BrowserTopic.BRIDGE_ALERT, payload, retain=False)

    def publish_bridge_status(
        self,
        bridge_start_time: float,
        api_consecutive_failures: int,
        api_failure_threshold: int,
        api_last_success_time: float,
        last_fetch_duration: float,
        write_total: int,
        write_success: int,
        write_failed: int,
        last_write_error: str | None,
        pending_write_count: int,
        mqtt_enabled_count: int,
        all_points_count: int,
        known_dynamic_count: int,
    ) -> None:
        """Publish a retained consolidated health snapshot to nibe/browser/bridge/status.

        This single topic gives an external monitor or automation everything
        it needs to assess bridge health without subscribing to multiple
        individual sensor topics or grepping logs.  Retained so the current
        state is immediately available to any new subscriber.
        """
        uptime_s = int(time.time() - bridge_start_time)
        api_healthy = api_consecutive_failures < api_failure_threshold

        payload = json.dumps(
            {
                # Overall state
                "status": "healthy" if api_healthy else "degraded",
                "timestamp": time.time(),
                "iso_timestamp": _fmt_ts(),
                "uptime_s": uptime_s,
                # API health
                "api": {
                    "healthy": api_healthy,
                    "consecutive_failures": api_consecutive_failures,
                    "failure_threshold": api_failure_threshold,
                    "last_success": _fmt_ts(api_last_success_time)
                    if api_last_success_time > 0
                    else None,
                    "last_fetch_duration_s": round(last_fetch_duration, 3),
                },
                # Write metrics
                "writes": {
                    "total": write_total,
                    "success": write_success,
                    "failed": write_failed,
                    "pending": pending_write_count,
                    "success_rate_pct": round(write_success / write_total * 100, 1)
                    if write_total > 0
                    else 100.0,
                    "last_error": last_write_error,
                },
                # Entity counts
                "entities": {
                    "total_known": all_points_count,
                    "mqtt_enabled": mqtt_enabled_count,
                    "known_dynamic": known_dynamic_count,
                },
            }
        )
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
            "identifiers": [f"{self.device_id}_management"],
            "name": f"{self.device_name} Management",
            "manufacturer": self.device_info.get("manufacturer", "NIBE"),
            "model": self.device_info.get("model", "Nibe S-series"),
            "serial_number": self.device_info.get("serial_number", ""),
        }
        mgmt_device = {k: v for k, v in mgmt_device.items() if v != ""}
        avail = MgmtTopic.AVAIL

        def _pub(topic: str, payload: dict) -> None:
            log_mqtt.debug("Publishing management discovery for %s", topic)  # pragma: no mutate
            self.mqtt.publish(topic, json.dumps(payload), retain=True)

        # One-time (idempotent) cleanup of the pre-refactor preset selector's
        # retained messages — see _LEGACY_PRESET_TOPICS.
        for _topic in _LEGACY_PRESET_TOPICS:
            self.mqtt.publish(_topic, "", retain=True)

        # KNOWN_DYNAMIC (nibe/browser/known_dynamic_points) was replaced by
        # DynamicPointMap (BrowserTopic.DYNAMIC_MAP) — clear any retained
        # message left over from a pre-DynamicPointMap install. The enum
        # member itself is kept (not moved into _LEGACY_PRESET_TOPICS) since
        # it documents what topic is being retired, not a removed family.
        self.mqtt.publish(BrowserTopic.KNOWN_DYNAMIC, "", retain=True)

        _pub(
            MgmtTopic.MODE_CONFIG,
            {
                "name": "Entity Mode",
                "unique_id": "nibe_active_mode",
                "state_topic": MgmtTopic.MODE_STATE,
                "availability_topic": avail,
                "device": mgmt_device,
                "icon": "mdi:tune",
                "entity_category": "diagnostic",
            },
        )
        # Read-only diagnostic — mode is config-level and restart-required
        # (see config.yaml / en.yaml), unlike the removed live preset
        # selector. Publish the current value immediately so it isn't
        # "Unknown" until the next reconciliation; EntityManager republishes
        # this whenever the applied mode actually changes.
        self.mqtt.publish(MgmtTopic.MODE_STATE, mode, retain=True)
        _pub(
            MgmtTopic.STATS_CONFIG,
            {
                "name": f"{self.device_name} Enabled Entity Stats",
                "unique_id": "nibe_entity_stats",
                "state_topic": MgmtTopic.STATS_STATE,
                "json_attributes_topic": MgmtTopic.STATS_ATTRS,
                "availability_topic": avail,
                "device": mgmt_device,
                "icon": "mdi:chart-box",
                "entity_category": "diagnostic",
                "state_class": "measurement",
                "unit_of_measurement": "entities",
            },
        )
        _pub(
            MgmtTopic.AID_CONFIG,
            {
                "name": "Aid Mode",
                "unique_id": "nibe_aid_mode",
                "state_topic": MgmtTopic.AID_STATE,
                "command_topic": MgmtTopic.AID_SET,
                "availability_topic": avail,
                "payload_on": "ON",
                "payload_off": "OFF",
                "device": mgmt_device,
                "icon": "mdi:alert-circle",
                "entity_category": "config",
            },
        )
        _pub(
            MgmtTopic.SMART_CONFIG,
            {
                "name": "Smart Mode",
                "unique_id": "nibe_smart_mode",
                "state_topic": MgmtTopic.SMART_STATE,
                "command_topic": MgmtTopic.SMART_SET,
                "availability_topic": avail,
                "options": ["normal", "away"],
                "device": mgmt_device,
                "icon": "mdi:home-account",
                "entity_category": "config",
            },
        )
        _pub(
            MgmtTopic.ALARM_CONFIG,
            {
                "name": f"{self.device_name} Active Alarms",
                "unique_id": "nibe_notifications",
                "state_topic": MgmtTopic.ALARM_STATE,
                "json_attributes_topic": MgmtTopic.ALARM_ATTRS,
                "availability_topic": avail,
                "device": mgmt_device,
                "icon": "mdi:bell-alert",
                "entity_category": "diagnostic",
                "state_class": "measurement",
                "unit_of_measurement": "alarms",
            },
        )
        _pub(
            MgmtTopic.ALARM_RESET_CONFIG,
            {
                "name": "Reset Alarms",
                "unique_id": "nibe_reset_alarms",
                "command_topic": MgmtTopic.ALARM_RESET_PRESS,
                "availability_topic": avail,
                "device": mgmt_device,
                "icon": "mdi:bell-off",
                "entity_category": "config",
            },
        )
        _pub(
            MgmtTopic.FORCE_POLL_CONFIG,
            {
                "name": "Force Poll",
                "unique_id": "nibe_force_poll",
                "command_topic": MgmtTopic.FORCE_POLL_PRESS,
                "availability_topic": avail,
                "device": mgmt_device,
                "icon": "mdi:refresh",
                "entity_category": "config",
            },
        )
        if mode == "menus":
            # Only makes sense when a Nibe Menus dashboard actually exists
            # to regenerate — see publish_management_discovery docstring.
            _pub(
                MgmtTopic.REGEN_DASH_CONFIG,
                {
                    "name": "Regenerate Dashboard",
                    "unique_id": "nibe_regen_dashboard",
                    "command_topic": MgmtTopic.REGEN_DASH_PRESS,
                    "availability_topic": avail,
                    "device": mgmt_device,
                    "icon": "mdi:view-dashboard-edit",
                    "entity_category": "config",
                },
            )
        else:
            # Clear any retained config left over from a previous menus-mode
            # run — otherwise HA keeps showing the button as a ghost entity
            # pointing at a regen action that no longer applies.
            self.mqtt.publish(MgmtTopic.REGEN_DASH_CONFIG, "", retain=True)
        _pub(
            MgmtTopic.UPTIME_CONFIG,
            {
                "name": f"{self.device_name} Bridge Uptime",
                "unique_id": "nibe_bridge_uptime",
                "state_topic": MgmtTopic.UPTIME_STATE,
                "json_attributes_topic": MgmtTopic.UPTIME_ATTRS,
                "availability_topic": avail,
                "device": mgmt_device,
                "icon": "mdi:clock-outline",
                "entity_category": "diagnostic",
                "device_class": "duration",
                "unit_of_measurement": "s",
                "state_class": "total_increasing",
            },
        )
        _pub(
            MgmtTopic.LAST_FETCH_CONFIG,
            {
                "name": "API Last Fetch",
                "unique_id": "nibe_last_fetch_timestamp",
                "state_topic": MgmtTopic.LAST_FETCH_STATE,
                "availability_topic": avail,
                "device": mgmt_device,
                "icon": "mdi:clock-check",
                "entity_category": "diagnostic",
                "device_class": "timestamp",
            },
        )
        _pub(
            MgmtTopic.FETCH_DUR_CONFIG,
            {
                "name": "API Fetch Duration",
                "unique_id": "nibe_fetch_duration",
                "state_topic": MgmtTopic.FETCH_DUR_STATE,
                "availability_topic": avail,
                "device": mgmt_device,
                "icon": "mdi:timer-sand",
                "entity_category": "diagnostic",
                "unit_of_measurement": "s",
                "device_class": "duration",
                "state_class": "measurement",
            },
        )
        _pub(
            MgmtTopic.API_OK_CONFIG,
            {
                "name": "API Reachable",
                "unique_id": "nibe_api_reachable",
                "state_topic": MgmtTopic.API_OK_STATE,
                "availability_topic": avail,
                "payload_on": "ON",
                "payload_off": "OFF",
                "device_class": "connectivity",
                "device": mgmt_device,
                "icon": "mdi:api",
                "entity_category": "diagnostic",
            },
        )

        if debug_mode:
            _pub(
                MgmtTopic.FLUSH_MAP_CONFIG,
                {
                    "name": "Flush Dynamic Map (DEBUG)",
                    "unique_id": "nibe_flush_dynamic_map",
                    "command_topic": MgmtTopic.FLUSH_MAP_PRESS,
                    "availability_topic": avail,
                    "device": mgmt_device,
                    "icon": "mdi:table-refresh",
                    "entity_category": "config",
                },
            )
            _pub(
                MgmtTopic.RUN_TESTS_CONFIG,
                {
                    "name": "Run Test Suite (DEBUG)",
                    "unique_id": "nibe_run_tests",
                    "command_topic": MgmtTopic.RUN_TESTS_PRESS,
                    "availability_topic": avail,
                    "device": mgmt_device,
                    "icon": "mdi:test-tube",
                    "entity_category": "config",
                },
            )
            # Sensor that shows last test run result
            _pub(
                f"{_HA_BASE}/sensor/nibe_test_suite_result/config",
                {
                    "name": "Test Suite Result (DEBUG)",
                    "unique_id": "nibe_test_suite_result",
                    "state_topic": MgmtTopic.RUN_TESTS_STATE,
                    "json_attributes_topic": MgmtTopic.RUN_TESTS_ATTRS,
                    "availability_topic": avail,
                    "device": mgmt_device,
                    "icon": "mdi:test-tube",
                    "entity_category": "diagnostic",
                },
            )
            _pub(
                MgmtTopic.TEST_CONNECTION_CONFIG,
                {
                    "name": "Test API Connection (DEBUG)",
                    "unique_id": "nibe_test_connection",
                    "command_topic": MgmtTopic.TEST_CONNECTION_PRESS,
                    "availability_topic": avail,
                    "device": mgmt_device,
                    "icon": "mdi:lan-connect",
                    "entity_category": "config",
                },
            )
            # Sensor that shows last connectivity check result
            _pub(
                f"{_HA_BASE}/sensor/nibe_connectivity_check_result/config",
                {
                    "name": "Connectivity Check Result (DEBUG)",
                    "unique_id": "nibe_connectivity_check_result",
                    "state_topic": MgmtTopic.TEST_CONNECTION_STATE,
                    "json_attributes_topic": MgmtTopic.TEST_CONNECTION_ATTRS,
                    "availability_topic": avail,
                    "device": mgmt_device,
                    "icon": "mdi:lan-connect",
                    "entity_category": "diagnostic",
                },
            )
        else:
            # Clear any retained debug-entity configs left over from a
            # previous debug-mode run — otherwise HA keeps showing them as
            # ghost entities pointing at debug actions that are unavailable.
            self.mqtt.publish(MgmtTopic.FLUSH_MAP_CONFIG, "", retain=True)
            self.mqtt.publish(MgmtTopic.RUN_TESTS_CONFIG, "", retain=True)
            self.mqtt.publish(f"{_HA_BASE}/sensor/nibe_test_suite_result/config", "", retain=True)
            self.mqtt.publish(MgmtTopic.TEST_CONNECTION_CONFIG, "", retain=True)
            self.mqtt.publish(
                f"{_HA_BASE}/sensor/nibe_connectivity_check_result/config", "", retain=True
            )

        # Initial sensor states
        self.mqtt.publish(MgmtTopic.UPTIME_STATE, "0", retain=True)
        self.mqtt.publish(MgmtTopic.API_OK_STATE, "ON", retain=True)
        self.mqtt.publish(MgmtTopic.FETCH_DUR_STATE, "0.00", retain=True)
        start_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.mqtt.publish(MgmtTopic.LAST_FETCH_STATE, start_iso, retain=True)

        # Reset test suite state on every startup so a 'running' state left by
        # an interrupted run (e.g. add-on rebuild mid-test) does not persist.
        if debug_mode:
            self.mqtt.publish(MgmtTopic.RUN_TESTS_STATE, "unknown", retain=True)
            self.mqtt.publish(
                MgmtTopic.RUN_TESTS_ATTRS,
                json.dumps(
                    {
                        "status": "unknown",
                        "note": "Reset at startup — previous run may have been interrupted.",
                    }
                ),
                retain=True,
            )

        # Mark management interface online
        self.mqtt.publish(MgmtTopic.AVAIL, "online", retain=True)

        # Publish device info for the frontend card
        self.mqtt.publish(
            BrowserTopic.DEVICE_INFO,
            json.dumps(
                {
                    "model": self.device_info.get("model", "Nibe S-series"),
                    "name": self.device_info.get("name", self.device_name),
                    "manufacturer": self.device_info.get("manufacturer", "NIBE"),
                    "serial": self.device_info.get("serial_number", ""),
                }
            ),
            retain=True,
        )
