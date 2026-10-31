"""
nibe_entity_manager.py
======================
EntityManager — owns the full lifecycle of Nibe data points as HA entities.

Responsibilities
----------------
- Point registry: indexing all discovered points keyed by variableId.
- Entity lifecycle: enabling/disabling entities by publishing/clearing
  MQTT discovery configs (delegated to MqttDiscoveryPublisher).
- Bulk polling: fetching all point values from the API and publishing state
  updates for active entities.
- Dynamic point detection: noticing when firmware exposes new registers
  at runtime (e.g. manual-mode setpoints) and auto-enabling them.
- Write command handling: decoding HA MQTT payloads, writing to the API,
  publishing optimistic state, reverting on failure.
- Persistent changelog: recording dynamic point appearances/disappearances
  in MQTT so the frontend card can display history across sessions.

What this module does NOT do
-----------------------------
- No raw HTTP (all API calls go through NibeApiClient).
- No topic string construction (all MQTT publishing through MqttDiscoveryPublisher).
- No HA registry watching or notification sending.
- No generic caching primitives — LRUCache lives in nibe_caching.py; it
  has no dependency on EntityManager and is reused as a plain data
  structure (e.g. entity-type detection caching).

Public surface
--------------
EntityManager(api_client, publisher, notify_fn, dismiss_fn, mqtt_client)
    .discover_points()
    .scan_mqtt_discovery()  → set[int]
    .restore_from_mqtt()    → int
    .apply_mode(name)
    .enable_entity(point_id) → bool
    .disable_entity(point_id) → bool
    .update_all_states(force=False)
    .resubscribe_all()
    .republish_availability()
    .mark_changelog_read()
    .complete_deferred_discovery(mode) → bool
    .publish_enabled_state()
    .get_memory_usage()    → dict (memory statistics)
    .all_points            → list (property)
    .active_entities       → list (property)
"""

import base64
import concurrent.futures
import dataclasses
import gzip
import hashlib
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import uuid
from collections import deque
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import date as _date
from datetime import timedelta as _timedelta
from typing import Any

import yaml
from nibe_attribution import (
    LateTail,
    ScanWindow,
    changed_candidates,
    controller_side_decision,
    late_arrival_decision,
    late_tail_until,
    learning_outcome,
    poll_verdict,
    recordable_value,
    version_changed,
)
from nibe_caching import LRUCache
from nibe_dynamic_map import DynamicPointEntry, DynamicPointMap
from nibe_entity_detection import (
    MODES,
    apply_divisor,
    apply_metadata_overrides,
    clean_string,
    create_entity_id,
    detect_entity_type,
    firmware_fingerprint,
    get_value_mapping,
    is_writable_point,
    meaningful_default,
    reverse_divisor,
)
from nibe_mqtt_publisher import BrowserTopic, MqttDiscoveryPublisher, t_attributes, t_config
from nibe_utils import atomic_write_text
from nibe_utils import fmt_ts as _fmt_ts

log_api = logging.getLogger("nibe.api")
log_mqtt = logging.getLogger("nibe.mqtt")
log_discovery = logging.getLogger("nibe.discovery")
log_restore = logging.getLogger("nibe.restore")
log_entities = logging.getLogger("nibe.entities")
log_commands = logging.getLogger("nibe.commands")
log_history = logging.getLogger("nibe.history")

# ── Module-level constants ─────────────────────────────────────────────────────
# Keeping all tuneable numbers here (rather than inline) makes it trivial to
# audit limits, and eliminates "what does 60 mean here?" questions in code review.

# Write commands
_CMD_ID_LENGTH = 8  # hex chars in a correlation ID (uuid4 prefix)
_TEXT_REGISTER_MAX_LEN = 64  # max chars for a Nibe string register

# Every HA domain publish_entity_discovery can publish a point under — the
# set of discovery topics a point's retained config could be sitting on when
# the bridge no longer knows which one it used (see
# _clear_retained_discovery).
_DISCOVERY_DOMAINS = (
    "sensor",
    "binary_sensor",
    "switch",
    "number",
    "select",
    "button",
    "text",
    "time",
)

# Post-write dynamic-point scan window
_POST_WRITE_SCAN_S = 90  # seconds to keep accelerated bulk polling
# Nibe firmware has ~60s internal cache refresh
# cycle; 90s gives comfortable margin.
_LATE_ARRIVAL_S = 180  # after a scan window, new points are still linked to
# its switch for this long (plus one poll interval) at the normal poll
# rate — see _late_arrival.
_LEARNING_WAIT_MARGIN_S = 5  # a write waits for a learning window this
# much longer than the window: the poll loop ends it at its next 5 s poll —
# see _wait_for_learning_window.

# Pending-write staleness: a write that has been *executing* this long is
# treated as timed-out. Measured from the moment the write executor picked
# the write up, never from when the command arrived — time spent queued
# behind other writes doesn't count, since the executor is single-worker
# (deliberately, to serialize writes) and a queue can legitimately be many
# minutes deep with nothing wrong. A write waiting for a learning window
# (_wait_for_learning_window) gets its start time only after that wait.
_STALE_WRITE_AGE_S = _POST_WRITE_SCAN_S + 10  # seconds

# How long a point must be *continuously* absent from the bulk response
# before its entity is disabled. Disabling is destructive — it clears the
# retained discovery config, so Home Assistant deletes the entity, resetting
# its history and breaking any dashboard or automation referencing it.
#
# A controller reboot or firmware update serves an incomplete point list for
# a while. Observed on real hardware during a 4.13.12 update: the API came
# back reporting 1145 of 1169 points, and every cooling-related point —
# including BT25, an essential sensor — was missing for about a minute across
# four consecutive polls before returning. Disabling on the first miss
# deleted all of them.
#
# Absence is still reported immediately, by publishing "offline" to the
# availability topic: "I cannot read this right now" is honest and
# non-destructive, and Home Assistant shows the entity as unavailable.
# Only the irreversible step waits for this grace period, which comfortably
# outlasts a reboot while still retiring a genuinely removed accessory in
# minutes rather than leaving it forever.
_ABSENT_GRACE_S = 300  # seconds

# Changelog
_CHANGELOG_MAX_ENTRIES = 500  # hard deque cap; time-based prune also runs
_CHANGELOG_MIN_ENTRIES = 50  # always keep at least this many entries
_CHANGELOG_PRUNE_S = 3600  # run time-based prune at most once per hour

# MQTT scan sentinel
_MQTT_SCAN_TIMEOUT_S = 15  # seconds to wait for the sentinel round-trip

# Applied-mode persistence (entity mode reconciliation across restarts)
_APPLIED_MODE_FILE = "/data/applied_mode"
_WANTED_POINTS_FILE = "/data/wanted_points.json"
_RECLASSIFIED_POINTS_FILE = "/data/reclassified_points.json"
_SNAPSHOTS_FILE = "/data/snapshots.json"
_FIRMWARE_RECORD_FILE = "/data/firmware_record.json"
# Running totals (total_increasing) — see _running_total_dropped.
_RUNNING_TOTALS_FILE = "/data/running_totals.json"
_RUNNING_TOTAL_RESET_S = 900  # a lower value lasting this long is a real reset
_RUNNING_TOTALS_SAVE_S = 3600  # how often the last values are written to disk
# Version registers whose change marks a firmware update: the controller
# (2509, EB100), the outdoor unit (2453, EB101), its inverter (14987) and an
# S135 (802). See _check_firmware_update.
_FIRMWARE_VERSION_POINTS = (2509, 2453, 14987, 802)
_SNAPSHOTS_MAX = 10  # maximum number of named snapshots
_APPLIED_MODE_TIMEOUT_S = 5  # seconds to wait for the retained applied_mode
# message — a single topic, not a full scan, so
# far shorter than _MQTT_SCAN_TIMEOUT_S. Only
# ever times out on the very first startup after
# this topic exists (never published before);
# every subsequent startup gets it retained.

# HA persistent notification IDs — centralised so dismissals and raises
# always use the same string and a typo can't leave a ghost notification.
_NOTIF_API_UNREACHABLE = "nibe_api_unreachable"
_NOTIF_WRITE_ERROR = "nibe_write_error"
_NOTIF_NO_ENTITIES = "nibe_no_entities"
_NOTIF_DISCOVERY_INCOMPLETE = "nibe_discovery_incomplete"
_NOTIF_ENTITY_IDS_CHANGED = "nibe_entity_ids_changed"
# _report_entity_id_changes: wait this long after the last type change before
# notifying (a startup republishes every changed point within seconds), and
# at most this long for Home Assistant to register the new entity ids.
_ID_CHANGE_QUIET_S = 60
_ID_CHANGE_MAX_WAIT_S = 600


# ── Changelog payload compression ─────────────────────────────────────────────
# The changelog history retained MQTT message can reach 58 KB at 90 days'
# retention.  gzip + base64 reduces it to ~2.3 KB (96% reduction).
#
# The payload is a plain ASCII string: "gzip1:<base64-encoded gzip bytes>".
# Using base64 (rather than raw bytes) ensures paho always publishes a clean
# UTF-8 string — publishing raw bytes caused Python's bytes repr (b'...') to
# appear in the MQTT message, which the frontend could not parse.
#

_GZIP_SENTINEL = "gzip1:"


def _compress_payload(data: dict) -> str:
    """Serialise *data* to JSON, gzip-compress, base64-encode, and prepend sentinel.

    Returns a plain ASCII string safe to publish via paho without any
    binary encoding surprises.
    """
    raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(raw, compresslevel=6)
    return _GZIP_SENTINEL + base64.b64encode(compressed).decode("ascii")


def _decompress_payload(payload: bytes | str) -> bytes:
    """Decompress a payload produced by ``_compress_payload``.

    Accepts either bytes (from paho's message.payload) or str.
    """
    if isinstance(payload, (bytes, bytearray)):
        text = payload.decode("utf-8", errors="replace")
    else:
        text = payload

    compressed = base64.b64decode(text[len(_GZIP_SENTINEL) :])
    return gzip.decompress(compressed)


# ============================================================================
# STARTUP MODE DECISION (pure — unit-testable without mocking main())
# ============================================================================


def _response_values(response: dict) -> dict[int, int | None]:
    """Each point's integerValue in a bulk response, keyed by point id
    (None for a point without one)."""
    values: dict[int, int | None] = {}
    for key, point in response.items():
        try:
            pid = int(key)
        except (TypeError, ValueError):
            continue
        values[pid] = (
            (point.get("value") or {}).get("integerValue") if isinstance(point, dict) else None
        )
    return values


def _known_mode_or_none(mode: str | None) -> str | None:
    """Return *mode* if this build recognises it, otherwise None.

    An unrecognised applied-mode record carries no usable information, so it
    must read as "unknown" rather than as a mode. decide_startup_action()
    treats any *known* value differing from the configured mode as a
    deliberate mode change and reconciles the enabled set — which under the
    default "replace" behaviour disables everything outside the new mode,
    including entities the user enabled by hand. Inferring that from a value
    we cannot even resolve to a point set would be destructive on the basis
    of nothing.

    Mapping it to None instead routes through the existing migration path:
    startup restores unchanged and then re-records the configured mode, so a
    corrupt or stale record repairs itself on the next start rather than
    persisting indefinitely.

    Real cause, found on live hardware: the "Run Test Suite" debug button
    runs the suite inside the add-on container, and a property test wrote
    its sentinel "__test_named_mode__" into /data/applied_mode. The test
    isolation for that is fixed separately (see tests/conftest.py); this
    makes the bridge resilient to such a record regardless of origin —
    a half-written file, or a mode removed in a later release.
    """
    if mode is None:
        return None
    if mode in MODES:
        return mode
    # pragma: no mutate start
    log_restore.warning(
        "Ignoring unrecognised applied-mode record %r — treating it as no record at all. "
        "The current mode will be re-recorded, and no entities will be disabled on its "
        "account. Known modes: %s",
        mode,
        ", ".join(sorted(MODES)),
    )
    # pragma: no mutate end
    return None


def decide_startup_action(
    has_existing_entities: bool,
    applied_mode: str | None,
    config_mode: str,
) -> str:
    """Decide which startup action to take under the entity-mode model.

    Pure and side-effect-free so the three-way branch that drives both
    main()'s startup sequence and complete_deferred_discovery() can be
    unit-tested directly, without mocking the API, MQTT broker, or the
    ~600-line main() function itself (which remains hardware-validated
    and deliberately not unit-tested).

    Returns one of:
      "apply"     — fresh install (no existing retained entities). Enable
                    exactly the config mode's point set.
      "restore"   — normal restart. Either the applied mode matches the
                    configured mode, or the applied mode is unknown (the
                    migration boundary — first startup after this feature
                    was deployed, or a wiped applied-mode record). Restore
                    from the broker unchanged; do not disable anything.
                    IMPORTANT: when applied_mode was None (the migration
                    case), the caller must also call
                    EntityManager.record_applied_mode(config_mode) after
                    restoring — this establishes the baseline so a genuine
                    mode change is detectable on a later restart. Without
                    it, read_applied_mode() returns None forever and mode
                    changes never reconcile. When applied_mode already
                    equalled config_mode, no extra call is needed (the
                    record is already correct).
      "reconcile" — a deliberate mode change was detected across a
                    restart (applied_mode is known and differs from
                    config_mode). Caller must restore_from_mqtt() first
                    (so mqtt_enabled_points and active_dynamic_points
                    reflect real state), then apply_mode(config_mode) to
                    reconcile to the new set.
    """
    if not has_existing_entities:
        return "apply"
    if applied_mode is None or applied_mode == config_mode:
        return "restore"
    return "reconcile"


# ============================================================================
# VALUE MAPPING TRANSLATIONS
# ============================================================================


def _format_version(point_id: int, raw_value: object) -> str:
    """A version register's raw value as its version string.

    2453 (EB101) packs major<<12 | minor<<6 | patch: 12481 → 3.3.1,
    confirmed from S2125-12 firmware 3.3.1. 2509 (SMO S40, EB100) packs
    major<<8 | minor: 1035 = 0x040B → 4.11; the patch level isn't in this
    register. Any other version register (14987, the inverter, is a plain
    u8 — 61 in myUplink as well) is its plain number.
    """
    try:
        v = int(raw_value)  # type: ignore[call-overload]
    except (ValueError, TypeError):
        return str(raw_value)
    if point_id == 2453:
        return f"{(v >> 12) & 0x3F}.{(v >> 6) & 0x3F}.{v & 0x3F}"
    if point_id == 2509:
        return f"{(v >> 8) & 0xFF}.{v & 0xFF}"
    return str(v)


_FINGERPRINT_LABELS = {
    "modbusRegisterType": "register",
    "modbusRegisterID": "Modbus register",
    "variableSize": "size",
    "minValue": "min",
    "maxValue": "max",
    "divisor": "divisor",
    "unit": "unit",
}


def _describe_fingerprint_change(old: dict, new: dict) -> str:
    """What changed between two firmware_fingerprint()s, e.g.
    "min 55 → 550, unit '' → '°C'"; empty when nothing did."""

    def show(field: str, value: object) -> str:
        if field == "modbusRegisterType" and isinstance(value, str):
            return value.removeprefix("MODBUS_").removesuffix("_REGISTER").lower()
        if field == "unit":
            return repr(value or "")
        return str(value)

    return ", ".join(
        f"{label} {show(field, old.get(field))} → {show(field, new.get(field))}"
        for field, label in _FINGERPRINT_LABELS.items()
        if old.get(field) != new.get(field)
    )


def _fingerprint_checksum(points: dict) -> str:
    """Checksum over the firmware record's points: ids and fingerprints only
    — titles follow the language and entity types are the bridge's own."""
    payload = json.dumps({key: entry[2] for key, entry in points.items()}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_point_ids_file(path: str) -> set[int]:
    """The point ids in a fallback file (a JSON list); empty when the file
    is missing or not such a list."""
    try:
        with open(path, encoding="utf-8") as f:
            loaded = json.loads(f.read())
        if isinstance(loaded, list):
            return {int(pid) for pid in loaded}
    except (OSError, ValueError, TypeError):
        pass
    return set()


def _load_value_mapping_translations(language: str | None) -> dict[str, str]:
    """Load the value_mappings: table from translations/<language>.yaml.

    nibe_entity_detection.py's VALUE_MAPPINGS dict is hardcoded English —
    that module must stay pure (no I/O, no state — see ARCHITECTURE.md), so it
    has no way to know the configured language itself. This loader is the
    other half: it reads this repo's existing translations/*.yaml files
    (previously only read by the HA Supervisor directly, for the add-on's
    config UI — see the Dockerfile's COPY translations/ comment) and
    returns a flat {english_label: translated_label} dict that
    _process_and_publish_state uses to translate a resolved VALUE_MAPPINGS
    label before publishing it as an entity's state.

    Scoped to the 22 languages this repo ships a translations/*.yaml for
    (bg/cs/da/de/en/es/et/fi/fr/hu/it/lt/lv/nl/no/pl/ro/ru/sk/sl/sv/uk) — not every language code
    config.yaml's schema accepts; a file is only added for a language with
    a NIBE source for its labels, so the others have none, config UI
    included.

    Returns {} (meaning: no translation, callers fall back to the English
    label unchanged) for language None/""/"en", for any language outside
    those, or if the file is missing/malformed/lacks a
    value_mappings: key — this must never raise or crash startup over a
    translation file problem.
    """
    if not language or language == "en":
        return {}

    candidates = [
        f"/translations/{language}.yaml",
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "translations", f"{language}.yaml"
        ),
    ]
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            mapping = data.get("value_mappings") if isinstance(data, dict) else None
            if isinstance(mapping, dict):
                # Both keys and values must be strings — a malformed file
                # (e.g. YAML parsing "no"/"yes" as booleans) must not crash
                # translation lookups later; skip non-string entries rather
                # than propagate them.
                return {
                    k: v for k, v in mapping.items() if isinstance(k, str) and isinstance(v, str)
                }
            return {}
        except OSError:
            continue
        except yaml.YAMLError:
            return {}
    return {}


# ============================================================================
# ENTITY MANAGER
# ============================================================================


class EntityManager:
    """Owns the full lifecycle of Nibe data points as HA MQTT entities.

    Parameters
    ----------
    api_client : NibeApiClient
        All HTTP calls to the Nibe API go through this object.
    publisher : MqttDiscoveryPublisher
        All MQTT publishing goes through this object.
    notify_fn : callable(mqtt, title, message, notification_id)
        Function to create a persistent HA notification.
    dismiss_fn : callable(mqtt, notification_id)
        Function to dismiss a persistent HA notification.
    mqtt_client :
        A connected paho MQTT client (used for command subscriptions and
        direct availability publishes that bypass the publisher).
    """

    def __init__(
        self,
        api_client: Any,
        publisher: MqttDiscoveryPublisher,
        notify_fn: Callable,
        dismiss_fn: Callable,
        mqtt_client: Any,
        # pragma: no mutate start — capacity tuning constant, no observable
        # behavioral difference at ±1
        max_cache_size: int = 5000,
        # pragma: no mutate end
        value_translations: dict[str, str] | None = None,
    ) -> None:
        self._api = api_client
        self._pub = publisher
        self._notify = notify_fn
        self._dismiss = dismiss_fn
        self.mqtt = mqtt_client

        # {english_label: translated_label} for nibe_entity_detection.py's
        # hardcoded VALUE_MAPPINGS labels — see _load_value_mapping_translations's
        # own docstring for why this can't live in that (pure) module
        # instead. Loaded once by the caller (main()) and passed in here —
        # not loaded internally — because MqttDiscoveryPublisher needs the
        # exact same dict for its select-entity options list, and a
        # select's state (here) and its published options (there) must
        # agree on the same translated strings or HA rejects/mismatches the
        # selection. Defaults to {} (no translation, English passthrough)
        # so every existing direct EntityManager(...) construction in tests
        # keeps working unmodified.
        self._value_translations: dict[str, str] = value_translations or {}

        # Performance optimization: Cache entity type detection results.
        # Metadata, title, and description are static within a single run
        # (firmware updates restart the bridge) so point_id alone is the key.
        self._entity_type_cache: LRUCache = LRUCache(
            max_size=2000
        )  # pragma: no mutate — capacity tuning constant, no observable behavioral difference at ±1

        # Points dynamically reclassified from binary_sensor -> sensor after
        # being observed reporting a raw value other than 0/1 (see
        # _reclassify_binary_sensor). Persisted (retained RECLASSIFIED_POINTS
        # topic plus _RECLASSIFIED_POINTS_FILE) and applied by
        # _get_cached_entity_type, so the correction survives restarts and a
        # dynamic point's reappearance instead of being re-derived from the
        # static metadata (which says binary_sensor again) every time. Also
        # gates the loud warning log to the reclassifying transition itself.
        self._binary_sensor_reclassified: set[int] = set()

        # Running totals: the last value published per point (divisor
        # applied), when a lower one was first seen, and when the values were
        # last written to _RUNNING_TOTALS_FILE — see _running_total_dropped.
        self._running_totals: dict[int, float] = {}
        self._running_total_low_since: dict[int, float] = {}
        self._running_totals_saved_at: float = 0.0

        # ── Point registry ────────────────────────────────────────────────────
        # Single source of truth: dict keyed by variableId.
        # .all_points property exposes list(values()) for callers that need a list.
        self.all_points_by_id: dict[int, dict] = {}

        # ── Active entity registry ────────────────────────────────────────────
        # Dict keyed by point_id; .active_entities property wraps it as a list.
        self.active_entities_by_id: dict[int, dict] = {}
        self._active_entities_lock = threading.Lock()

        # ── EntityManager state lock ──────────────────────────────────────────
        # Reentrant lock that serialises mutations to mqtt_enabled_points,
        # last_states, and active_dynamic_points across three concurrent
        # callers:
        #   • write executor  (_handle_command_worker)
        #   • management executor (enable/disable via ManagementCommandHandler)
        #   • watcher thread  (_on_entity_enabled / _on_entity_disabled)
        # RLock is required because apply_mode, _publish_dynamic_changes, and
        # _reconcile_dynamic_points all acquire this lock and then call
        # enable_entity / disable_entity, which also acquire it.
        #
        # NOTE: all_points_by_id and baseline_point_ids are NOT fully covered
        # by this lock. _enable_entity_locked/_disable_entity_locked read and
        # (via _index_point/_deindex_point) mutate all_points_by_id under this
        # lock, but _fetch_bulk_data mutates both attributes directly from the
        # main poll thread WITHOUT acquiring it, so this lock cannot give full
        # mutual exclusion against the poll thread for those two attributes. In
        # practice this is tolerable only because CPython's GIL makes each
        # individual dict/set get/set/add/discard atomic; anything doing more
        # than a single such op against all_points_by_id or baseline_point_ids
        # from a non-poll thread must not assume this lock is sufficient.
        self._em_lock = threading.RLock()

        # ── Post-write scan lock ──────────────────────────────────────────────
        # Protects _window and _late_tail, replaced by the write executor and
        # the poll loop (and Force Poll on the management thread).
        self._post_write_lock = threading.Lock()

        # ── Enabled-state tracking ────────────────────────────────────────────
        self.mqtt_enabled_points: set[int] = set()

        # Set post-construction from cfg.mode_switch_behavior, same pattern as
        # api_failure_threshold/changelog_retention_days below. "replace"
        # (default) is apply_mode()'s original prune-to-exact-set behaviour;
        # "merge" only ever adds the new mode's points, never disables
        # anything already enabled — see apply_mode() for the actual branch.
        self.mode_switch_behavior: str = "replace"

        # ── Dynamic point tracking ────────────────────────────────────────────
        # DynamicPointMap replaces the former flat known_dynamic_points set.
        # It records, for every writable switch/select, which values cause
        # dynamic points to appear/disappear in the firmware's bulk fetch.
        # Loaded from the retained DYNAMIC_MAP MQTT topic at startup (see
        # _setup_dynamic_map_loading), with /data/dynamic_point_map.json as
        # a filesystem fallback (see discover_points(), which calls
        # dynamic_point_map.from_file() when nothing was restored from MQTT).
        # Persisted write-through to both stores on every change — MQTT via
        # _setup_dynamic_map_loading's publish path, the file via
        # _persist_dynamic_map()/DynamicPointMap.to_file(). Actual I/O is
        # delegated to this class (EntityManager), not performed inside
        # DynamicPointMap itself — see that module's own docstring.
        self.dynamic_point_map: DynamicPointMap = DynamicPointMap()

        # Reverse lookup: point_id → (menu_id, menu_title) built from
        # menu_structure.yaml at dashboard provisioning time. Used to include
        # menu location in dynamic change notifications.
        self.point_to_menu_map: dict[int, tuple[str, str]] = {}
        # Currently active dynamic point_ids — the set that should be enabled
        # in HA right now.  Persisted to MQTT (ACTIVE_DYNAMIC topic) so restarts
        # resume the correct active set without re-running discovery.
        self.active_dynamic_points: set[int] = set()
        # True once the startup action has put the enabled set in place —
        # before that, a point without an entity may simply not be restored
        # yet, so _resend_dynamic_removals must not clear its config.
        self._enabled_set_restored = False
        self.baseline_point_ids: set[int] = set()
        # point_id -> when it was first seen missing from the bulk response,
        # cleared the moment it comes back. Gates the destructive disable in
        # _update_entity_state; see _ABSENT_GRACE_S for why that wait exists.
        self._absent_since: dict[int, float] = {}
        # Enabled points (retained discovery config on the broker) that were
        # missing from the bulk response at startup, so restore_from_mqtt
        # could not rebuild them: point_id -> when that was found. They keep
        # their retained config — the absence may be a controller that is
        # still coming up — and get the same _ABSENT_GRACE_S as a runtime
        # absence via _expire_absent_at_startup. Without this they were never
        # cleaned up at all: the runtime grace path only covers active
        # entities, so a point a firmware update removed while the bridge was
        # down stayed an unavailable entity in HA forever.
        self._absent_at_startup: dict[int, float] = {}
        self.initial_discovery_complete: bool = False

        # ── Firmware updates (_check_firmware_update) ─────────────────────────
        # The version registers' raw values and the static point list
        # ({id: [title, type]}) as of the last firmware version, persisted to
        # _FIRMWARE_RECORD_FILE; None until loaded. Compared only once the
        # point set has been unchanged for _ABSENT_GRACE_S
        # (_point_set_stable_since), since a restarting controller serves a
        # partial list for a minute — the same across several polls.
        self._firmware_record: dict | None = None
        # ── Entity ids changed by a type change (_report_entity_id_changes) ───
        # point_id -> {old_domain, new_domain, title, old_id, since}, collected
        # from the publisher's type_changes. entity_id_resolver is the
        # registry watcher's entity_id_for, set at startup once it exists;
        # until then no entity id is known.
        self.entity_id_resolver: Callable[[int], str | None] = lambda _pid: None
        self._id_changes: dict[int, dict] = {}
        self._id_changes_last: float = 0.0
        self._firmware_record_loaded: bool = False
        self._point_set_last: set[int] = set()
        self._point_set_stable_since: float = 0.0
        # Cheap per-poll change detection for _check_firmware_update: the
        # identity of each point's metadata object (_fetch_bulk_data only
        # replaces it when its content changed), the dynamic points checked
        # since the last change, and whether a full comparison is due (also
        # once per process start).
        self._metadata_identity: int = 0
        self._dynamic_checked: set[int] = set()
        self._firmware_check_due: bool = True
        self._dynamic_changes: list[dict] = []

        # ── Wanted points (catch-all re-enable safety net) ────────────────────
        # Points explicitly enabled by the user (via enable_entity, apply_mode,
        # or restore_snapshot) that should reappear automatically if they ever
        # drop out of a bulk fetch and later come back — regardless of whether
        # they were ever registered as a dynamic point. This complements
        # dynamic_point_map's predictive, controller-driven tracking (which
        # only ever registers switch/select controllers) with a reactive,
        # causality-agnostic net for points disabled by the generic
        # "absent from bulk data" fallback in _update_entity_state. See
        # ARCHITECTURE.md §6a for the design writeup and the bug report
        # (GitHub issue #21) this was built to fix.
        # Loaded from the retained WANTED_POINTS MQTT topic at startup (see
        # _setup_dynamic_map_loading), with _WANTED_POINTS_FILE as a
        # filesystem fallback (see discover_points()).
        self._wanted_points: set[int] = set()
        # Forces one _reconcile_wanted_points run on the first successful poll
        # after startup; after that it only runs on a fetch that contains a
        # point the previous fetch didn't (see update_all_states).
        self._wanted_reconcile_pending: bool = True

        # ── Write metrics (Finding 3) ─────────────────────────────────────────
        # Monotonically increasing counters for the lifetime of this bridge session.
        # Published as part of the stats payload and the bridge/status topic so
        # automations and external monitors can track write health over time.
        self._write_total: int = 0
        self._write_success: int = 0
        self._write_failed: int = 0
        self._last_write_error: str | None = None  # human-readable last error
        self.pending_writes: dict[int, dict] = {}
        self._pending_writes_lock = threading.Lock()
        self._write_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="nibe_write"
        )
        # Writes submitted to _write_executor that haven't finished yet
        # (queued or currently running). Read by _submit_write to tell a
        # caller how many writes are ahead of it; see that method for why
        # this is tracked explicitly rather than inferred from scan state.
        self._write_queue_lock = threading.Lock()
        self._write_queue_depth: int = 0
        # Set by begin_shutdown() to break out of long in-flight waits on the
        # write worker so the executors can actually drain within the
        # shutdown budget. Never cleared — a shutdown is terminal.
        self._shutdown_event = threading.Event()

        # ── Post-write scan ───────────────────────────────────────────────────
        # The open scan window, or None (see ScanWindow). While one is open
        # the poll loop polls every post_write_interval instead of
        # bulk_interval, so the points a switch shows surface quickly, and
        # links them to the window's point. Opened by an HA write
        # (_open_post_write_scan) or a change on the controller
        # (_open_learning_window); ended by update_all_states.
        self._window: ScanWindow | None = None
        self.post_write_interval: int = 5
        self._post_write_duration: int = _POST_WRITE_SCAN_S
        # Set when the point set changes in a learning window or the window
        # ends. A write that opens a scan window waits for it, keeping one
        # value learned at a time (_wait_for_learning_window).
        self._learning_window_ended = threading.Event()
        self._learning_window_ended.set()
        # Every (point, value) whose change on the controller opened a window
        # in this run: each opens one at most once, so a switch the controller
        # flips by itself can't keep the poll loop at 5 s
        # (_controller_side_change).
        self._controller_side_tried: set[tuple[int, int]] = set()
        # Points a switch shows that arrive after its scan window — see
        # _late_arrival. Neither this nor a controller-side window is used
        # until _attribution_blocked_until after a failed poll, a partial
        # point list or a firmware version change.
        self._late_tail: LateTail | None = None
        self._attribution_blocked_until: float = 0.0

        # ── Bulk data cache ───────────────────────────────────────────────────
        self.bulk_data: dict[int, dict] = {}

        # Cache for cleaned title/description strings.  The Nibe API never
        # changes these between firmware updates, so running clean_string on
        # every point every poll is wasted work.  Keyed by point_id; value is
        # (raw_title, raw_description, cleaned_title, cleaned_description).
        # Invalidated automatically when the raw string from the API changes.
        self._point_string_cache = LRUCache(max_size=max_cache_size)

        # ── Misc state ────────────────────────────────────────────────────────
        self.last_states: dict[int, str] = {}
        # The last availability payload sent per availability topic, and the
        # points whose retained state was cleared (sentinel / out of range):
        # each poll used to send every entity's "online" again, and flip a
        # sentinel entity online → offline and clear its state again — about
        # 1,150 messages a poll for 909 entities, each one processed by Home
        # Assistant. Now only a change is sent (_publish_availability,
        # _clear_state_once). Forgotten on disable and after a reconnect.
        self._availability_sent: dict[str, str] = {}
        self._state_cleared: set[int] = set()
        # Points whose state was published since the MQTT connection was
        # made: an unchanged state is sent once more after a reconnect, as
        # the broker may have lost its retained messages.
        self._state_sent: set[int] = set()
        self.last_bulk_fetch: float = 0  # pragma: no mutate — sentinel "never fetched"; ±1s is negligible against a real epoch timestamp
        self.bulk_interval: int = 30
        self.api_failure_threshold: int = 3
        self.last_fetch_duration: float = 0.0
        self.bridge_start_time: float = time.time()
        self._on_enabled_state_change: Callable | None = None
        self._last_published_enabled: frozenset[int] = frozenset()
        self.api_consecutive_failures: int = 0
        self.api_last_success_time: float = time.time()
        self._bulk_fetch_lock = threading.Lock()
        self.published_configs: set[int] = set()

        # ── Device mode cache ─────────────────────────────────────────────────
        # Avoids a redundant fetch_device_info() call on every poll cycle just
        # to read aidMode and smartMode.  Set dirty=True after a write so the
        # next poll refreshes from the API.
        self.device_modes_cache: dict[str, str] = {}
        self.device_modes_dirty: bool = True
        # When device_modes_cache was last filled from the API. The cache
        # also expires after nibe_ha_integration._DEVICE_MODES_MAX_AGE_S (see _publish_device_modes):
        # dirty is only ever set by the bridge's own aid/smart mode writes, so
        # without an expiry a mode changed on the controller itself (or from
        # another app) never reached HA until the bridge restarted.
        self.device_modes_fetched_at: float = 0.0
        # Incremented under _em_lock every time a write handler sets
        # device_modes_dirty = True. _publish_device_modes captures this
        # before its unlocked fetch_device_info() call and compares it
        # after — if a write landed while the fetch was in flight, the
        # fetched response may predate that write, so it must not be
        # allowed to clear the dirty flag a concurrent writer just set.
        self.device_modes_write_seq: int = 0

        # ── Notification flags ────────────────────────────────────────────────
        self._api_notification_active: bool = False
        # True until this process's first successful bulk fetch — see there.
        self._api_first_success_pending: bool = True
        # What republish_retained_state needs that only startup knows: the
        # arguments management discovery was published with, and the
        # applied mode (its /data file may not survive a container rebuild).
        self._mgmt_discovery_args: tuple[str, bool] | None = None
        # Run once complete_deferred_discovery has restored the entities —
        # see HAEntityRegistryWatcher._reconcile_user_disabled.
        self._after_deferred_discovery: list[Callable[[], None]] = []
        self._applied_mode_known: str | None = None
        self._alarm_notification_active: bool = False
        # The alarms the current notification lists (see update_alarm_state),
        # so a change in the set re-sends it rather than leaving it stale.
        self._alarm_notified_ids: tuple | None = None
        # True until the first successful alarm poll: a notification left from
        # before a restart is unknown to this process and must still be cleared.
        self._alarm_first_poll_pending: bool = True
        self._last_alarm_count: int = -1  # -1 forces first log
        self._last_stats_key: tuple | None = None
        self._write_notification_active: bool = False
        self._discovery_notification_active: bool = False
        self._range_warnings_issued: set[int] = set()
        # Points already warned about a missing state_topic — without this,
        # a point stuck in this state (never reaches last_states, since the
        # warning path returns before recording it) would re-warn every
        # single poll cycle indefinitely instead of once.
        self._missing_state_topic_warned: set[int] = set()

        # ── Incremental stats counters ────────────────────────────────────────
        self._stats_type_counts: dict[str, int] = {}
        self._stats_category_counts: dict[str, int] = {}
        self._stats_writable_count: int = 0

        # ── Enabled-state publish suppression ─────────────────────────────────
        self._suppress_enabled_state_depth = 0
        self._suppress_lock = threading.Lock()

        # ── Changelog ─────────────────────────────────────────────────────────
        # deque with a hard cap prevents unbounded growth even if _prune_changelog
        # is somehow skipped.  appendleft() is O(1) vs list.insert(0, …) O(n).
        # Time-based pruning still runs on startup and periodically (hourly) to
        # evict entries older than changelog_retention_days.
        self.change_history: deque = deque(maxlen=_CHANGELOG_MAX_ENTRIES)
        self.changelog_retention_days: int = 90
        self._history_seq: int = 0
        # See _point_catalog_changed / _flush_point_catalog.
        self._point_catalog_dirty = False
        self._last_published_seq: int = 0
        self._last_prune_time: float = 0.0  # for hourly prune cadence

        # Populated by main() after the API response is available
        self.device_info: dict = {}
        # Kept so on_connect callbacks can call resubscribe_all / republish_availability
        self._mgmt_avail_topic: str | None = None
        # Management topic subscriptions registered by create_management_handlers().
        # Each entry is (topic: str, handler: callable, qos: int).
        # resubscribe_all() replays these after a broker reconnect so that
        # aid/smart mode, alarm reset, force poll, enable/disable, and
        # changelog-read buttons keep working after Mosquitto restarts.
        self._mgmt_subscriptions: list[tuple] = []
        self._mgmt_subscriptions_lock = threading.Lock()

        # Serializes the load-modify-save sequence in save_snapshot/
        # delete_snapshot — mgmt_executor has multiple workers, so two
        # snapshot commands issued close together could otherwise both load
        # the same on-disk snapshots.json, each apply their own change in
        # memory, and write back with the second write silently clobbering
        # the first (a lost update).
        self._snapshots_lock = threading.Lock()

        self._setup_history_loading()
        self._setup_dynamic_map_loading()

    # ------------------------------------------------------------------ #
    # Public list views                                                    #
    # ------------------------------------------------------------------ #

    @property
    def post_write_active(self) -> bool:
        """Whether a post-write scan window is open (_window)."""
        return self._window is not None

    @property
    def all_points(self) -> list:
        """Read-only list view of all known points."""
        return list(self.all_points_by_id.values())

    @property
    def active_entities(self) -> list:
        """Read-only list view of active entities."""
        return list(self.active_entities_by_id.values())

    # ------------------------------------------------------------------ #
    # Suppression context manager                                          #
    # ------------------------------------------------------------------ #

    @contextmanager
    def _suppress_enabled_state(self) -> Generator[None, None, None]:
        """Prevent publish_enabled_state() from firing inside a bulk operation."""
        with self._suppress_lock:
            self._suppress_enabled_state_depth += 1
        try:
            yield
        finally:
            with self._suppress_lock:
                self._suppress_enabled_state_depth -= 1

    def _is_suppressed(self) -> bool:
        with self._suppress_lock:
            return self._suppress_enabled_state_depth > 0

    # ------------------------------------------------------------------ #
    # Point index                                                          #
    # ------------------------------------------------------------------ #

    def _index_point(self, point: dict) -> None:
        point_id = point["variableId"]
        if "metadata" in point:
            point["metadata"] = apply_metadata_overrides(point_id, point["metadata"])
        self.all_points_by_id[point_id] = point

    def _deindex_point(self, point_id: int) -> None:
        self.all_points_by_id.pop(point_id, None)
        self._point_string_cache.pop(point_id)

    # ------------------------------------------------------------------ #
    # Discovery                                                            #
    # ------------------------------------------------------------------ #

    def _point_catalog_changed(self) -> None:
        """Tell the card the set or shape of its points changed: the point
        list now, and the bulk all_metadata batch once at the end of this
        poll (_flush_point_catalog).

        Both are retained, and Home Assistant doesn't deliver them to a
        freshly loaded card in any fixed order relative to each other or to
        the per-point meta/{id} topics. all_metadata used to be published
        only at discovery, so whenever it arrived last it overrode fresher
        state: a reclassified point went back to binary_sensor in the card
        (observed against HA 2026.9), and a point deindexed since startup
        could be added back after the point list had pruned it.

        all_metadata is deferred rather than sent here because it is about
        half a megabyte and some callers run once per point — an accessory
        install or a firmware update changes dozens of points in one poll.
        """
        self._pub.publish_point_list(self.all_points_by_id)
        self._point_catalog_dirty = True

    def _flush_point_catalog(self) -> None:
        """Publish all_metadata if anything changed it since the last flush."""
        if self._point_catalog_dirty:
            self._point_catalog_dirty = False
            self._pub.publish_all_metadata(self.all_points)

    def discover_points(self) -> bool:
        """Fetch all points from the API and establish the static baseline.

        The baseline is the set of points present at startup.  Subsequent
        bulk fetches compare against it to identify dynamic points.

        After establishing the baseline:
        1. Populates DynamicPointMap with any writable switches/selects not
           yet in the table (first run: all; subsequent: incremental).
        2. Reconciles dynamic point state against persisted active set.
        """
        log_discovery.info("Initial point discovery...")  # pragma: no mutate

        # If the DynamicPointMap is empty after MQTT loading, try file fallback
        if len(self.dynamic_point_map) == 0:
            file_count = self.dynamic_point_map.from_file()
            if file_count:
                # pragma: no mutate start
                log_discovery.info(
                    "DynamicPointMap loaded from file fallback: %d entries", file_count
                )
                # pragma: no mutate end

        # Same fallback for _wanted_points, if MQTT restore hasn't populated it yet.
        if not self._wanted_points:
            self._wanted_points = _load_point_ids_file(_WANTED_POINTS_FILE)
            # pragma: no mutate start
            if self._wanted_points:
                log_discovery.info(
                    "Wanted points loaded from file fallback: %d entries",
                    len(self._wanted_points),
                )
            # pragma: no mutate end

        # Same fallback for the reclassified set — it must be in place before
        # the fetch below classifies (and indexes) every point.
        if not self._binary_sensor_reclassified:
            self._binary_sensor_reclassified = _load_point_ids_file(_RECLASSIFIED_POINTS_FILE)

        # Before the first publish: a controller that restarts along with the
        # bridge (a power cut) can serve a placeholder 0 straight away.
        self._load_running_totals()

        if not self._fetch_bulk_data(detect_changes=False):
            log_discovery.error("Initial discovery failed")  # pragma: no mutate
            return False

        self.baseline_point_ids = {p["variableId"] for p in self.all_points}
        self.initial_discovery_complete = True
        # pragma: no mutate start
        log_discovery.info("Baseline established: %d static points", len(self.baseline_point_ids))
        # pragma: no mutate end

        # Resolve entity types for all indexed points so populate_from_bulk
        # can filter to switches and selects only.
        entity_types = {pid: pt.get("entity_type", "") for pid, pt in self.all_points_by_id.items()}
        added = self.dynamic_point_map.populate_from_bulk(self.all_points_by_id, entity_types)
        if added:
            # pragma: no mutate start
            log_discovery.info("DynamicPointMap: populated %d new skeleton entries", added)
            # pragma: no mutate end
            self._persist_dynamic_map()

        # Both judge presence against the whole bulk response, not the static
        # baseline: known dynamic points are deliberately left out of the
        # baseline, so passing it marked every dynamic switch/select's own
        # entry firmware_removed on every startup while it was right there
        # in the response — and the restore could never clear it. A point
        # shown by a dynamic select then stopped being expected, so the
        # startup reconcile deleted its live entity as stale.
        bulk_point_ids = set(self.bulk_data)

        # Restore firmware_removed status for any entries that reappeared
        self.dynamic_point_map.restore_from_bulk(bulk_point_ids)

        # Mark entries whose point_id is no longer in the firmware's bulk
        # response as firmware_removed — suppresses future learning-mode
        # probes on switches/selects the firmware has removed.
        newly_removed = self.dynamic_point_map.mark_absent_as_firmware_removed(bulk_point_ids)
        if newly_removed:
            # pragma: no mutate start
            log_discovery.info(
                "DynamicPointMap: %d point(s) marked firmware_removed (absent from bulk): %s",
                len(newly_removed),
                sorted(newly_removed),
            )
            # pragma: no mutate end
            self._persist_dynamic_map()

        # Reconcile dynamic point active state against persisted ACTIVE_DYNAMIC
        self._reconcile_dynamic_points()

        self._pub.publish_all_metadata(self.all_points)
        self._pub.publish_point_list(self.all_points_by_id)
        return True

    def complete_deferred_discovery(self, initial_mode: str) -> bool:
        """Complete a discovery that failed at startup (device was unreachable).

        Called from the main polling loop on the first successful bulk fetch.
        Replays the full initialisation sequence — baseline, scan MQTT, then
        the same three-way startup decision main() uses (see
        decide_startup_action) — so entities appear exactly as on a normal
        startup, including a deliberate mode change detected across a
        restart that only takes effect once the device answers again.
        """
        # pragma: no mutate start
        log_discovery.info("API is back — completing deferred startup discovery...")
        # pragma: no mutate end

        device_response = self._api.fetch_device_info()
        if device_response:
            self.device_info = _build_device_info(
                device_response, self._pub.device_id, self._pub.device_name, self._api.base_url
            )
            self._pub.device_info = self.device_info
            # pragma: no mutate start
            log_discovery.info(
                "Device info updated: model=%s, serial=%s",
                self.device_info.get("model"),
                self.device_info.get("serial_number"),
            )
            # pragma: no mutate end
        else:
            # pragma: no mutate start
            log_discovery.warning("Could not fetch device info — serial/firmware will be blank")
            # pragma: no mutate end

        if not self.discover_points():
            # pragma: no mutate start
            log_discovery.warning("Deferred discovery fetch failed — will retry next poll")
            # pragma: no mutate end
            return False

        mqtt_enabled = self.scan_mqtt_discovery()
        applied_mode = self.read_applied_mode() if mqtt_enabled else None
        action = decide_startup_action(
            has_existing_entities=bool(mqtt_enabled),
            applied_mode=applied_mode,
            config_mode=initial_mode,
        )

        if action == "apply":
            # pragma: no mutate start
            log_restore.info(
                "Deferred MQTT scan found 0 existing discovery configs — "
                "looks like a fresh install. Applying mode '%s'.",
                initial_mode,
            )
            # pragma: no mutate end
        elif action == "restore":
            log_restore.info("Restoring entities from MQTT database...")  # pragma: no mutate
        else:
            # pragma: no mutate start
            log_restore.info(
                "Deferred discovery: applied mode differs from configured mode '%s' — "
                "restoring then reconciling to the new mode.",
                initial_mode,
            )
            # pragma: no mutate end

        self.apply_startup_action(action, applied_mode, initial_mode)

        self.publish_enabled_state()
        # pragma: no mutate start
        log_discovery.info(
            "Deferred discovery complete: %d points, %d enabled, %d active",
            len(self.all_points),
            len(self.mqtt_enabled_points),
            len(self.active_entities),
        )
        # pragma: no mutate end
        for callback in list(self._after_deferred_discovery):
            try:
                callback()
            except Exception as e:  # noqa: BLE001 — best-effort; logged and degrades gracefully
                # pragma: no mutate start
                log_discovery.warning("After-deferred-discovery callback failed: %s", e)
                # pragma: no mutate end
        return True

    def apply_startup_action(
        self,
        action: str,
        applied_mode: str | None,
        initial_mode: str,
    ) -> None:
        """Execute the restore/apply/reconcile mutations for a startup action.

        This is the shared core called by both complete_deferred_discovery()
        and _execute_startup_action() in generate_nibe_mqtt.py.  Callers are
        responsible for their own context-specific log messages; this method
        performs only the state mutations.

        apply     — fresh install: enable the configured mode.
        restore   — same mode as before: restore from MQTT, optionally
                    establish applied-mode baseline if record is missing.
        reconcile — mode changed: restore then apply new mode.
        """
        if action == "apply":
            self.apply_mode(initial_mode)
        elif action == "restore":
            self.restore_from_mqtt()
            if applied_mode is None:
                # pragma: no mutate start
                log_restore.info(
                    "No applied-mode record found — establishing baseline at "
                    "current mode '%s' (existing entities left unchanged).",
                    initial_mode,
                )
                # pragma: no mutate end
                self.record_applied_mode(initial_mode)
        else:  # "reconcile"
            self.restore_from_mqtt()
            self.apply_mode(initial_mode)
        self._enabled_set_restored = True
        self._resend_dynamic_removals()

    # ------------------------------------------------------------------ #
    # MQTT scan / restore                                                  #
    # ------------------------------------------------------------------ #

    def scan_mqtt_discovery(self) -> set[int]:
        """Scan the broker for retained HA discovery configs.

        Uses a sentinel message to detect end-of-retained-messages reliably
        rather than a fixed sleep, with a hard timeout fallback.
        """
        _SENTINEL_TOPIC = BrowserTopic.SCAN_SENTINEL

        log_discovery.debug("Scanning MQTT for existing discovery configs...")  # pragma: no mutate
        discovered_points: set[int] = set()
        sentinel_received = threading.Event()

        def on_config(_client: Any, _userdata: Any, message: Any) -> None:
            topic = message.topic
            if topic.startswith("homeassistant/") and topic.endswith("/config") and message.payload:
                try:
                    config = json.loads(message.payload.decode("utf-8"))
                    # This topic also carries other integrations' configs, in
                    # any shape: valid JSON need not be an object, nor its
                    # unique_id a string. Not ours, so not worth a warning.
                    unique_id = config.get("unique_id") if isinstance(config, dict) else None
                    if isinstance(unique_id, str) and unique_id.startswith("nibe_"):
                        id_str = unique_id[5:]
                        if id_str.isdigit():
                            point_id = int(id_str)
                            discovered_points.add(point_id)
                            # Pre-seed the publisher's dedup cache with this
                            # retained config's own hash, so restore_from_mqtt()
                            # (called right after this scan) can genuinely skip
                            # republishing configs that haven't changed — the
                            # cache would otherwise always be empty on restart,
                            # since the publisher instance is newly constructed
                            # every process start. See seed_config_hash_from_retained.
                            self._pub.seed_config_hash_from_retained(point_id, message.payload)
                            # Pre-seed the publisher's entity-type-change
                            # tracking from this retained config's own topic,
                            # so a point whose classification changed between
                            # restarts (e.g. an ENTITY_TYPE_OVERRIDES update)
                            # gets its stale previous-domain topic cleared
                            # instead of left behind as an orphaned ghost
                            # entity. topic is "homeassistant/<entity_type>/
                            # <entity_id>/config" — see t_config(). See
                            # seed_entity_type_from_retained.
                            retained_entity_type = topic.split("/")[1]
                            self._pub.seed_entity_type_from_retained(point_id, retained_entity_type)
                except Exception as e:  # noqa: BLE001 — an escaping exception kills paho's thread
                    # pragma: no mutate start
                    log_discovery.warning("Could not parse config from %s: %s", topic, e)
                    # pragma: no mutate end

        def on_sentinel(_client: Any, _userdata: Any, _message: Any) -> None:
            sentinel_received.set()

        config_topic = "homeassistant/+/+/config"
        self.mqtt.subscribe(config_topic)
        self.mqtt.message_callback_add(config_topic, on_config)
        self.mqtt.subscribe(_SENTINEL_TOPIC)
        self.mqtt.message_callback_add(_SENTINEL_TOPIC, on_sentinel)
        self.mqtt.publish(_SENTINEL_TOPIC, "scan", retain=False)

        sentinel_ok = sentinel_received.wait(timeout=_MQTT_SCAN_TIMEOUT_S)
        if not sentinel_ok:
            # pragma: no mutate start
            log_discovery.warning(
                "Sentinel timeout after %ds — retained message delivery may be incomplete. "
                "%d configs received so far; some entities may be missing until the next restart.",
                _MQTT_SCAN_TIMEOUT_S,
                len(discovered_points),
            )
            # pragma: no mutate end

        self.mqtt.message_callback_remove(config_topic)
        self.mqtt.message_callback_remove(_SENTINEL_TOPIC)
        self.mqtt.unsubscribe(config_topic)
        self.mqtt.unsubscribe(_SENTINEL_TOPIC)

        with self._em_lock:
            self.mqtt_enabled_points.clear()
            self.mqtt_enabled_points.update(discovered_points)
        # pragma: no mutate start
        log_discovery.debug(
            "Found %d existing MQTT discovery configs%s",
            len(discovered_points),
            " (scan may be partial — sentinel timed out)" if not sentinel_ok else "",
        )
        # pragma: no mutate end
        return discovered_points

    def restore_from_mqtt(self) -> int:
        """Rebuild active_entities from the set found by scan_mqtt_discovery().

        Re-publishes every discovery config so code changes take effect on
        restart without requiring a manual disable/re-enable cycle.
        Only republishes configs whose hash has changed since the last run,
        reducing MQTT traffic on restarts where nothing has changed.
        """
        # This rebuilds the entire enabled set from the broker, so rebuild the
        # stats counts from zero alongside it. Points enabled before the scan
        # — dynamic points re-activated by _reconcile_dynamic_points inside
        # discover_points(), or wanted points re-enabled by a poll before a
        # deferred discovery completes — were already counted once, and
        # restore counting them again inflated the stats sensor.
        self._stats_type_counts = {}
        self._stats_category_counts = {}
        self._stats_writable_count = 0

        if not self.mqtt_enabled_points:
            log_restore.info("No existing MQTT configs found")  # pragma: no mutate
            return 0

        # pragma: no mutate start
        log_restore.info("Restoring %d entities from MQTT...", len(self.mqtt_enabled_points))
        # pragma: no mutate end
        restored_count = 0
        republished = 0
        failed_points = []

        for point_id in self.mqtt_enabled_points:
            point = self.all_points_by_id.get(point_id)
            if not point:
                # pragma: no mutate start
                log_restore.info(
                    "Point %d is in the enabled list but has no metadata — "
                    "it may be a dynamic point not currently active, "
                    "or it was removed in a firmware update. Skipping restore.",
                    point_id,
                )
                # pragma: no mutate end
                failed_points.append(point_id)
                # Only a real absence starts the grace clock. With no bulk
                # data at all (controller unreachable at startup) every point
                # looks missing; timing them from now would let a first
                # answer that is incomplete — a controller still booting —
                # delete everything it omits the moment it arrives, if the
                # outage lasted longer than the grace period. Deferred
                # discovery runs this restore again once the controller
                # answers, and tracks real absences from then.
                if self.bulk_data:
                    self._absent_at_startup[point_id] = time.time()
                continue

            entity_info = self._pub.publish_entity_discovery(point, self.bulk_data)
            if entity_info:
                with self._active_entities_lock:
                    prev = self.active_entities_by_id.get(point_id)  # pragma: no mutate
                    self.active_entities_by_id[point_id] = entity_info
                restored_count += 1
                # pragma: no mutate start
                if prev is None:
                    republished += 1
                # pragma: no mutate end

                if point["is_dynamic"]:  # every indexed point carries it
                    self.active_dynamic_points.add(point_id)

                if entity_info["command_topic"]:

                    def _make_handler(ei: dict) -> Callable:
                        def handler(_client: Any, _userdata: Any, message: Any) -> None:
                            self._handle_command(ei, message)

                        return handler

                    self.mqtt.subscribe(entity_info["command_topic"], qos=1)
                    self.mqtt.message_callback_add(
                        entity_info["command_topic"], _make_handler(entity_info)
                    )

                self._increment_stats(point)

                # Publish online for all restored entities so HA doesn't
                # show them as unavailable on startup. State will be updated
                # on the first bulk fetch poll.
                self.mqtt.publish(entity_info["availability_topic"], "online", retain=True)

            else:
                failed_points.append(point_id)

        if failed_points:
            # pragma: no mutate start
            log_restore.warning("Failed to restore %d points", len(failed_points))
            # pragma: no mutate end
            for point_id in failed_points:
                self.mqtt_enabled_points.discard(point_id)

        # Backfill the wanted-points safety net from what was just restored.
        #
        # _wanted_points was previously only ever written at the moment of an
        # explicit enable (enable_entity / apply_mode / restore_snapshot), so
        # a point enabled before that feature existed — or one whose record
        # was lost — came back enabled but unprotected. That silently defeated
        # _reconcile_wanted_points: the generic "absent from bulk data"
        # disable deliberately keeps a point wanted so it is re-enabled when
        # it reappears, but with nothing in the set the reconciliation never
        # even runs (its caller skips it when _wanted_points is empty).
        #
        # Observed on real hardware: a firmware update briefly served an
        # incomplete point list, thirteen entities were disabled, the points
        # returned a minute later, and none were re-enabled.
        #
        # Anything in the broker's enabled list was enabled deliberately at
        # some point, by a user or by a mode — that is exactly what this set
        # is meant to record. A subsequent mode change still un-marks what it
        # disables (see apply_mode), so a reconcile-on-startup sequence stays
        # correct: restore backfills, then apply_mode prunes.
        # Points absent at startup count too: they were in the broker's
        # enabled list just the same, and marking them is what gets them
        # re-enabled (by _reconcile_wanted_points) if they come back.
        newly_wanted = (
            self.mqtt_enabled_points | set(self._absent_at_startup)
        ) - self._wanted_points
        if newly_wanted:
            self._wanted_points.update(newly_wanted)
            self._persist_wanted_points()
            # pragma: no mutate start
            log_restore.info(
                "Marked %d restored point(s) as wanted so they are re-enabled "
                "automatically if the firmware drops them temporarily",
                len(newly_wanted),
            )
            # pragma: no mutate end

        # pragma: no mutate start
        log_restore.info(
            "Restored %d/%d entities (%d configs republished)",
            restored_count,
            len(self.mqtt_enabled_points),
            republished,
        )
        # pragma: no mutate end

        return restored_count

    # ------------------------------------------------------------------ #
    # Enable / disable                                                     #
    # ------------------------------------------------------------------ #

    def enable_entity(self, point_id: int) -> bool:
        """Publish an MQTT discovery config for a point, making it visible in HA.

        Marks point_id as user-wanted (see _wanted_points) so it is
        automatically re-enabled if it later disappears from a bulk fetch
        and reappears — regardless of whether it's dynamic-tracked.
        """
        with self._em_lock:
            result = self._enable_entity_locked(point_id)
            if result:
                self._mark_wanted(point_id)
            return result

    def _increment_stats(self, point: dict) -> None:
        """Update incremental type/category/writable stats for a point that
        just became enabled. Shared by _enable_entity_locked and
        restore_from_mqtt so the nibe_entity_stats HA sensor's counts stay
        correct on both a fresh enable and a restart-time restore."""
        entity_type_key = point.get("entity_type", "unknown")
        category_key = point.get("entity_category", "none")
        self._stats_type_counts[entity_type_key] = (
            self._stats_type_counts.get(entity_type_key, 0) + 1
        )
        self._stats_category_counts[category_key] = (
            self._stats_category_counts.get(category_key, 0) + 1
        )
        if point.get(
            "is_writable", False
        ):  # pragma: no mutate — None and False are both falsy here
            self._stats_writable_count += 1

    def _decrement_stats(self, point: dict) -> None:
        """Update incremental type/category/writable stats for a point that
        just became disabled. Mirror of _increment_stats, clamped at 0."""
        entity_type_key = point.get("entity_type", "unknown")
        category_key = point.get("entity_category", "none")
        if entity_type_key in self._stats_type_counts:
            self._stats_type_counts[entity_type_key] = max(
                0, self._stats_type_counts[entity_type_key] - 1
            )
        if category_key in self._stats_category_counts:
            self._stats_category_counts[category_key] = max(
                0, self._stats_category_counts[category_key] - 1
            )
        if point.get(
            "is_writable", False
        ):  # pragma: no mutate — None and False are both falsy here
            self._stats_writable_count = max(0, self._stats_writable_count - 1)

    def _enable_entity_locked(self, point_id: int) -> bool:
        """Implementation of enable_entity — caller must hold _em_lock."""
        point = self.all_points_by_id.get(point_id)
        if not point:
            # pragma: no mutate start
            log_entities.info(
                "Cannot enable point %d: not in bulk data "
                "(conditional point absent for this firmware/accessory configuration?)",
                point_id,
            )
            # pragma: no mutate end
            return False

        if point_id in self.mqtt_enabled_points:
            # pragma: no mutate start
            log_entities.debug(
                "Point %d is already enabled (discovery config exists) — skipping",
                point_id,
            )
            # pragma: no mutate end
            return True

        entity_info = self._pub.publish_entity_discovery(point, self.bulk_data)
        if not entity_info:
            return False

        with self._active_entities_lock:
            self.active_entities_by_id[point_id] = entity_info
        self.mqtt_enabled_points.add(point_id)

        if entity_info["command_topic"]:

            def command_handler(_client: Any, _userdata: Any, message: Any) -> None:
                self._handle_command(entity_info, message)

            self.mqtt.subscribe(entity_info["command_topic"], qos=1)
            self.mqtt.message_callback_add(entity_info["command_topic"], command_handler)

        self._increment_stats(point)

        self.mqtt.publish(entity_info["availability_topic"], "online", retain=True)
        # Only call _update_entity_state if bulk data is available for this point.
        # If it isn't (e.g. first enable before the first successful API poll),
        # skip the state publish here — the next poll will handle it.
        # Calling _update_entity_state when the point is absent from bulk_data
        # triggers its auto-disable path, immediately undoing the enable.
        if entity_info["point_id"] in self.bulk_data:
            self._update_entity_state(entity_info)

        if not self._is_suppressed():
            self.publish_enabled_state()

        # Dismiss the "no entities enabled" notification on the first successful enable.
        if self.mqtt and len(self.mqtt_enabled_points) == 1:
            self._dismiss(self.mqtt, _NOTIF_NO_ENTITIES)

        log_entities.info("Enabled point %d", point_id)  # pragma: no mutate
        return True

    def disable_entity(self, point_id: int, remove_from_wanted: bool = True) -> bool:
        """Remove the MQTT discovery config for a point, hiding it from HA.

        A manual disable through this public entry point (e.g. via the
        Entity Manager card, or mirroring an HA-side registry disable) is
        explicit user intent and must override the "firmware-state-driven,
        not mode-driven" active_dynamic_points bookkeeping apply_mode relies
        on for its own to_disable exclusion (see apply_mode's docstring and
        its `protected = set(self.active_dynamic_points)`). Without this, a
        manually-disabled dynamic point would stay in active_dynamic_points,
        so _reconcile_dynamic_points() would silently re-enable it on the
        next restart (its controlling switch hasn't changed, so it's still
        in expected_active) — undoing the user's choice with no indication
        why.

        This clearing lives here, not in _disable_entity_locked, because
        that lower-level helper has several other internal callers
        (apply_mode, _publish_dynamic_changes, _reconcile_dynamic_points)
        that already own active_dynamic_points bookkeeping and already
        persist it themselves at a point of their choosing — those callers
        must not inherit this side effect.

        remove_from_wanted defaults to True for explicit user-initiated
        disables (the normal case for this public entry point). The one
        caller that passes False is _update_entity_state's generic
        "absent from bulk data" fallback — that disable is a consequence of
        the point vanishing from the firmware's bulk response, not user
        intent, so the point must stay in _wanted_points and be picked back
        up by _reconcile_wanted_points() if it reappears.
        """
        with self._em_lock:
            result = self._disable_entity_locked(point_id)
            if point_id in self.active_dynamic_points:
                self.active_dynamic_points.discard(point_id)
                self._persist_active_dynamic()
            if remove_from_wanted:
                self._unmark_wanted(point_id)
            return result

    def _disable_entity_locked(self, point_id: int) -> bool:
        """Implementation of disable_entity — caller must hold _em_lock."""
        # A disabled entity is no longer polled, so a reappearance of its
        # point would never clear its absence record — and after a later
        # re-enable, the next brief absence (a controller reboot) would be
        # measured from that stale first miss and destroy the entity at
        # once instead of after _ABSENT_GRACE_S.
        self._absent_since.pop(point_id, None)
        if point_id not in self.mqtt_enabled_points:
            return True

        with self._active_entities_lock:
            entity_info = self.active_entities_by_id.pop(point_id, None)

        if entity_info:
            config_topic = t_config(entity_info["entity_type"], entity_info["entity_id"])
            self.mqtt.publish(config_topic, "", retain=True)
            # Invalidate the cached config hash so the next enable unconditionally
            # republishes the discovery config.  HA removes the entity when the
            # config is cleared, so the hash is stale and must not suppress the
            # next publish.
            self._pub.invalidate_config_hash(point_id)

            if entity_info.get("attributes_topic"):
                self.mqtt.publish(entity_info["attributes_topic"], "", retain=True)

            if entity_info.get("command_topic"):
                self.mqtt.message_callback_remove(entity_info["command_topic"])
                self.mqtt.unsubscribe(entity_info["command_topic"])

        self.mqtt_enabled_points.discard(point_id)
        # active_dynamic_points is deliberately NOT touched here — see
        # disable_entity()'s docstring for why that clearing belongs at the
        # public entry point, not this shared internal helper.
        self.last_states.pop(point_id, None)
        self._state_cleared.discard(point_id)
        self._state_sent.discard(point_id)
        if entity_info and entity_info.get("availability_topic"):
            self._availability_sent.pop(entity_info["availability_topic"], None)
        self._point_string_cache.pop(point_id)
        self._entity_type_cache.pop(
            point_id, None
        )  # pragma: no mutate — LRUCache.pop's own default=None makes this equivalent to a 1-arg call
        self._missing_state_topic_warned.discard(point_id)
        # A write triggered just before disable would otherwise leak here
        # forever: pending_writes is only ever cleared by _update_entity_state
        # (which stops running for this point once it's disabled) or an
        # explicit write failure — neither fires for a point that's been
        # disabled mid-flight.
        with self._pending_writes_lock:
            self.pending_writes.pop(point_id, None)

        point = self.all_points_by_id.get(point_id, {})
        self._decrement_stats(point)

        if not self._is_suppressed():
            self.publish_enabled_state()

        log_entities.info("Disabled point %d", point_id)  # pragma: no mutate
        return True

    def record_applied_mode(self, mode_name: str) -> None:
        """Record mode_name as the current applied mode WITHOUT touching the
        enabled entity set.

        Used exactly once per install at the migration boundary: when
        decide_startup_action() returns "restore" because no applied-mode
        record exists yet (read_applied_mode() returned None — either the
        first startup after this feature was deployed, or a wiped record).
        That case is deliberately non-destructive (spec §14) — the existing
        broker-curated enabled set is adopted as-is rather than reconciled
        against a possibly-unrelated historical preset. This call is what
        actually establishes the baseline so a *genuine* mode change on a
        later restart can be detected; without it, read_applied_mode()
        would return None forever and mode changes would never reconcile.
        """
        self._persist_applied_mode(mode_name)

    def apply_mode(self, mode_name: str) -> None:
        """Reconcile the enabled entity set to the target mode's point set.

        Unlike the old apply_preset (strictly additive), this both enables
        points newly required by the mode and disables points that are
        currently enabled but not part of the mode. Active dynamic points
        are never touched — their existence is firmware-state-driven, not
        mode-driven, so a mode change must not delete a live dynamic entity.

        Called in two situations only (see decide_startup_action /
        generate_nibe_mqtt.py's startup sequence):
          - Fresh install: enabled set starts empty, so nothing is disabled —
            behaviourally identical to the old additive apply_preset.
          - A deliberate mode change detected across a restart: the enabled
            set is first rebuilt from the broker (restore_from_mqtt), then
            reconciled here to the newly selected mode.
        It is never called on an ordinary same-mode restart, so manually
        curated additions (via the Entity Manager card) survive normal
        restarts and are only pruned when the mode itself changes — and
        even then, only when self.mode_switch_behavior == "replace" (the
        default). When it's "merge", nothing already enabled is ever
        disabled by a mode change; the mode's point set is only ever added
        to what's already active. See the `mode_switch_behavior` config
        option.
        """
        log_restore.info("Applying mode: %s", mode_name)  # pragma: no mutate

        mode_value = MODES.get(mode_name)
        if mode_value is None and mode_name == "all":
            target = set(self.all_points_by_id.keys())
        else:
            target = set(mode_value) if mode_value else set()
        # Only points that actually exist in this firmware's bulk data.
        target &= set(self.all_points_by_id.keys())

        protected = set(self.active_dynamic_points)
        to_enable = target - self.mqtt_enabled_points
        if self.mode_switch_behavior == "merge":
            to_disable: set[int] = set()
        else:
            to_disable = (self.mqtt_enabled_points - target) - protected

        # Suppress publish_enabled_state() for the duration of the enable/disable
        # loop so that each individual enable_entity/disable_entity call doesn't
        # trigger an intermediate publish. Use a single atomic check-and-increment
        # rather than _is_suppressed() + a separate lock acquisition — the two-step
        # pattern has a TOCTOU window where another thread could change the depth
        # between the read and the increment, leaving depth miscounted.
        with self._em_lock:
            with self._suppress_lock:
                was_suppressed = self._suppress_enabled_state_depth > 0
                if not was_suppressed:
                    self._suppress_enabled_state_depth += 1  # pragma: no mutate

            try:
                for point_id in to_enable:
                    if self._enable_entity_locked(point_id):
                        self._mark_wanted(point_id)
                for point_id in to_disable:
                    self._disable_entity_locked(point_id)
                    # Mode-driven disable is intentional user override, not a
                    # firmware-absence artifact — undoing it via
                    # _reconcile_wanted_points would fight the mode switch.
                    self._unmark_wanted(point_id)
                if self.mode_switch_behavior != "merge":
                    # Points that were enabled but absent at startup (see
                    # _absent_at_startup) aren't in mqtt_enabled_points, so
                    # to_disable never includes them — yet the mode excludes
                    # them just the same. Left alone they stay wanted and keep
                    # their retained config, and would be re-enabled on return
                    # despite the new mode.
                    for point_id in (set(self._absent_at_startup) - target) - protected:
                        self._clear_retained_discovery(point_id)
                        self._pub.invalidate_config_hash(point_id)
                        self._unmark_wanted(point_id)
                        del self._absent_at_startup[point_id]
                    # Likewise points disabled after the absence grace period:
                    # still wanted (so a return re-enables them), but not
                    # enabled, so to_disable never covers them either.
                    # Dynamic points are left to the dynamic path.
                    for point_id in self._excluded_wanted_points(target, protected):
                        self._unmark_wanted(point_id)
            finally:
                if not was_suppressed:
                    with self._suppress_lock:
                        self._suppress_enabled_state_depth -= 1

        self.publish_enabled_state()
        self._persist_applied_mode(mode_name)
        # pragma: no mutate start
        log_restore.info(
            "Mode '%s' applied: %d enabled, %d disabled (%d total)",
            mode_name,
            len(to_enable),
            len(to_disable),
            len(self.mqtt_enabled_points),
        )
        # pragma: no mutate end

    # ------------------------------------------------------------------ #
    # State updates                                                        #
    # ------------------------------------------------------------------ #

    def update_all_states(self, force: bool = False) -> None:
        """Poll the Nibe API and publish state updates for all active entities.

        Parameters
        ----------
        force :
            When True, bypass the bulk interval check and always fetch fresh
            data from the API. Used by the Force Poll management button.
        """
        current_time = time.time()

        # last_bulk_fetch is also reassigned (to 0) by resubscribe_all() on
        # paho's MQTT network thread after a broker reconnect — _em_lock
        # serializes that write against this method's own read/write of the
        # same attribute rather than leaving them as an unsynchronized race.
        # Only the quick read/compare/write is under the lock; the slow
        # _fetch_bulk_data() call below runs outside it so a reconnect isn't
        # blocked for the duration of an in-flight API fetch.
        with self._em_lock:
            if force:
                self.last_bulk_fetch = 0
            last_bulk_fetch = self.last_bulk_fetch

        # Use faster polling during the post-write scan window so dynamic
        # point changes surface quickly after a switch write.
        # Outside that window use the normal user-configured poll interval.
        ended: ScanWindow | None = None
        with self._post_write_lock:
            window = self._window
            if window is not None and current_time > window.until:
                ended = window
                self._window = None
                if window.point is not None:
                    ended_value = (self.bulk_data.get(window.point) or {}).get("raw_value")
                    if ended_value is not None:
                        self._start_late_tail(
                            window.point, int(ended_value), window.controller_side, after=0
                        )
                # pragma: no mutate start
                log_commands.info("Post-write scan window ended for point %s", window.point)
                # pragma: no mutate end
            _post_write_now_active = self._window is not None
        if ended is not None and ended.point is not None and ended.learning_value is not None:
            self._finish_learning_window(ended.point, ended.learning_value, ended.saw_points)

        effective_interval = (
            self.post_write_interval if _post_write_now_active else self.bulk_interval
        )

        # Per-step durations, logged once per poll at DEBUG ("Poll timing").
        timing: dict[str, float] = {}
        if (current_time - last_bulk_fetch) >= effective_interval:
            failures_before = self.api_consecutive_failures
            known_count = len(self.dynamic_point_map.all_known_dynamic_point_ids())
            should_detect = bool(known_count) or _post_write_now_active
            # The previous successful fetch's point set (_fetch_bulk_data
            # replaces it with the new one on success).
            previous_point_ids = set(self.published_configs)
            fetch_started = time.perf_counter()
            result = self._fetch_bulk_data(detect_changes=should_detect)
            timing["fetch"] = time.perf_counter() - fetch_started
            lock_was_busy = result is False and self.api_consecutive_failures == failures_before
            if not lock_was_busy:
                with self._em_lock:
                    self.last_bulk_fetch = current_time
                current_point_ids = set(self.bulk_data.keys())
                if result and self.initial_discovery_complete:
                    self._restore_reappeared_map_entries(current_point_ids)
                if result and self.initial_discovery_complete and self._absent_at_startup:
                    self._expire_absent_at_startup(current_point_ids)
                if result and self.initial_discovery_complete:
                    started = time.perf_counter()
                    self._check_firmware_update(current_point_ids)
                    timing["firmware check"] = time.perf_counter() - started
                started = time.perf_counter()
                self._report_entity_id_changes()
                timing["entity id check"] = time.perf_counter() - started
                # A wanted point can only need re-enabling if it has just come
                # back, i.e. this fetch holds an id the previous one didn't —
                # judged by set difference, not size: a point leaving and
                # another returning in the same poll leaves the count unchanged
                # (or smaller) while still bringing a point back. Not on every
                # poll, so a point left wanted while intentionally disabled
                # can't be fought over by the poll loop. Plus once after
                # startup, for points the startup itself found missing.
                # Gated on initial_discovery_complete: before a deferred
                # discovery finishes the point index is still empty, so a run
                # there could do nothing useful — and would use up the
                # one-time startup run that the first real poll needs.
                if (
                    result
                    and self.initial_discovery_complete
                    and self._wanted_points
                    and (self._wanted_reconcile_pending or current_point_ids - previous_point_ids)
                ):
                    self._wanted_reconcile_pending = False
                    self._reconcile_wanted_points(current_point_ids)

        if not self.active_entities_by_id:
            self._flush_point_catalog()
            return

        # pragma: no mutate start
        log_entities.debug("Updating %d active entities", len(self.active_entities_by_id))
        # pragma: no mutate end
        with self._active_entities_lock:
            snapshot = list(self.active_entities_by_id.values())

        started = time.perf_counter()
        for entity_info in snapshot:
            self._update_entity_state(entity_info)
        timing["entity updates"] = time.perf_counter() - started
        if time.time() - self._running_totals_saved_at >= _RUNNING_TOTALS_SAVE_S:
            self._save_running_totals()
        # Once per poll, after the bulk fetch and every entity update — the
        # two places that change the card's points.
        started = time.perf_counter()
        self._flush_point_catalog()
        timing["catalog flush"] = time.perf_counter() - started
        # pragma: no mutate start
        log_entities.debug(
            "Poll timing: %s",
            ", ".join(f"{step} {seconds * 1000:.0f} ms" for step, seconds in timing.items()),
        )
        # pragma: no mutate end

    def _restore_reappeared_map_entries(self, current_point_ids: set[int]) -> None:
        """Clear firmware_removed on map entries whose point is back.

        discover_points marks an entry firmware_removed when its point is
        absent from the startup bulk response, and used to be the only place
        that restored one. A switch/select that is itself dynamic is absent
        at startup whenever whatever shows it is off — so once it was
        switched back on, its learned outcomes were ignored on writes, it was
        never probed, and the menu dashboard didn't inject what it shows,
        all until the next restart happened to catch it present.
        """
        with self._em_lock:
            restored = self.dynamic_point_map.restore_from_bulk(current_point_ids)
        if restored:
            # pragma: no mutate start
            log_discovery.info(
                "DynamicPointMap: %d point(s) back in bulk, no longer firmware_removed: %s",
                len(restored),
                sorted(restored),
            )
            # pragma: no mutate end
            self._persist_dynamic_map()

    def _update_entity_state(self, entity_info: dict) -> None:
        """Read the cached bulk value for one entity and publish its HA state."""
        point_id = entity_info["point_id"]

        if entity_info["entity_type"] == "button":
            self.mqtt.publish(entity_info["availability_topic"], "online", retain=True)
            return

        with self._pending_writes_lock:
            pending_entry = self.pending_writes.get(point_id)
            if pending_entry:
                # Age from when the write actually started executing, not from
                # when the command arrived. The write executor is single-worker,
                # so a write can sit in the queue arbitrarily long behind other
                # writes' detection windows while nothing at all is wrong — ageing
                # from arrival evicted those healthy queued writes, which dropped
                # this very guard and let the pre-write value be republished (the
                # HA toggle visibly flipping back before the write had even run).
                # A write that has not started yet is never stale, however long
                # it has been waiting; only one the worker picked up and never
                # finished is.
                started_at = pending_entry.get("started_at")
                if started_at is not None and time.time() - started_at > _STALE_WRITE_AGE_S:
                    # pragma: no mutate start
                    log_commands.warning(
                        "Pending write [%s] for point %d is %ds old — "
                        "the write executor may be stuck. Evicting stale entry.",
                        pending_entry.get("cmd_id", "?"),
                        point_id,
                        int(time.time() - started_at),
                    )
                    # pragma: no mutate end
                    self.pending_writes.pop(point_id)
                    pending_entry = None
                else:
                    # Check whether the bulk API value now matches what was
                    # written.  If so, the controller has committed — clear the
                    # pending entry so normal state publishing resumes.
                    bulk_raw = self.bulk_data.get(point_id, {}).get("raw_value")
                    if bulk_raw is not None and bulk_raw == pending_entry.get("value"):
                        # pragma: no mutate start
                        log_commands.debug(
                            "Pending write [%s] for point %d confirmed by API "
                            "(raw=%s) — releasing hold",
                            pending_entry.get("cmd_id", "?"),
                            point_id,
                            bulk_raw,
                        )
                        # pragma: no mutate end
                        self.pending_writes.pop(point_id)
                        pending_entry = None
            pending = pending_entry is not None

        if pending:
            return

        # One read: a Force Poll on another thread can remove the point at any
        # moment, so a check followed by an index could raise KeyError.
        data = self.bulk_data.get(point_id)
        if data is None:
            if point_id in self.mqtt_enabled_points:
                with self._post_write_lock:
                    window = self._window
                known_dynamic = self.dynamic_point_map.all_known_dynamic_point_ids()
                if window is not None and (
                    point_id in known_dynamic or point_id in self.active_dynamic_points
                ):
                    # A known dynamic point absent during a post-write scan is
                    # disappearing. Route through _publish_dynamic_changes so it
                    # is deindexed, its MQTT meta is cleared, the changelog is
                    # updated, and the frontend is notified — not just disabled.
                    # _fetch_bulk_data has already removed the known dynamic
                    # points that were active; this catches the rest, e.g.
                    # active ones the map no longer knows after a flush.
                    # Any other point takes the absence grace path below, write
                    # or no write: a static point briefly missing within 90s of
                    # any write (a controller restart, a partial response) used
                    # to be disabled on the spot, deleting its HA entity.
                    if point_id in self.active_dynamic_points:
                        # pragma: no mutate start
                        log_entities.info(
                            "Point %d absent during post-write scan — "
                            "treating as dynamic disappearance",
                            point_id,
                        )
                        # pragma: no mutate end
                        self.baseline_point_ids.discard(point_id)
                        self._publish_dynamic_changes(
                            [],
                            {point_id},
                            window.point,
                            controller_side=window.controller_side,
                        )
                else:
                    now = time.time()
                    with self._em_lock:
                        first_absent = self._absent_since.setdefault(point_id, now)
                    newly_absent = first_absent == now

                    # Report the point as unreadable straight away. This is
                    # non-destructive: HA shows the entity unavailable and
                    # keeps it, its history and every reference to it.
                    self._publish_availability(entity_info, "offline")

                    if now - first_absent < _ABSENT_GRACE_S:
                        if newly_absent:
                            # pragma: no mutate start
                            log_entities.info(
                                "Point %d absent from bulk data — marking unavailable; will "
                                "disable only if still absent after %ds (a controller reboot "
                                "or firmware update serves an incomplete point list)",
                                point_id,
                                _ABSENT_GRACE_S,
                            )
                            # pragma: no mutate end
                        return

                    # pragma: no mutate start
                    log_entities.info(
                        "Point %d absent from bulk data for %ds — disabling entity",
                        point_id,
                        int(now - first_absent),
                    )
                    # pragma: no mutate end
                    # Mirrors the post-write branch's own discard above: a
                    # point indexed as static (is_dynamic=False) — because it
                    # once appeared outside a scan window before its
                    # controlling switch/select was ever learned — stays in
                    # baseline_point_ids forever otherwise, since only a
                    # bridge restart recomputes that set from scratch.
                    # Without this discard, the appearance-detection guard at
                    # `point_id not in self.baseline_point_ids` (see
                    # _fetch_bulk_data) would permanently block this point
                    # from ever being routed through the dynamic-learning
                    # path again — even after a correct, HA-driven write to
                    # its real controlling point re-opens a scan window.
                    self.baseline_point_ids.discard(point_id)
                    self.disable_entity(point_id, remove_from_wanted=False)
                    # Five minutes of continuous absence means the firmware no
                    # longer serves this point, so stop presenting it as one
                    # that exists — same cleanup as a dynamic disappearance in
                    # _publish_dynamic_changes. Left indexed, the card kept
                    # listing it until the next restart, and enabling it
                    # (enable_entity / restore_snapshot both treat
                    # all_points_by_id as "exists in firmware") created an
                    # entity that could only go unavailable and be disabled
                    # again. Must follow the disable, which reads the point's
                    # metadata for its stats bookkeeping. If the point comes
                    # back, _fetch_bulk_data re-indexes it before
                    # _reconcile_wanted_points runs, so the wanted-points
                    # re-enable still works.
                    with self._em_lock:
                        self._deindex_point(point_id)
                    self.mqtt.publish(
                        BrowserTopic.META_TEMPLATE.format(id=point_id), "", retain=True
                    )
                    self._point_catalog_changed()
            return

        # Present again (or never missing): forget any absence so a later gap
        # starts its grace period from scratch rather than inheriting an old
        # one and disabling the point almost immediately.
        if self._absent_since:
            with self._em_lock:
                self._absent_since.pop(point_id, None)

        if not data["is_ok"]:
            self._publish_availability(entity_info, "offline")
            return

        raw_value = data["raw_value"]
        string_value = data["string_value"]
        metadata = data.get("metadata", {})

        self._process_and_publish_state(entity_info, raw_value, string_value, metadata)

    def _running_total_dropped(self, entity_info: dict, raw_value: int, metadata: dict) -> bool:
        """Whether a running total (published total_increasing: energy,
        operating hours, compressor starts) is lower than its last published
        value, and should be published unavailable instead.

        A restarting controller can serve 0 for a lifetime counter for about
        a minute (GitHub issue #132); Home Assistant took the return to the
        real value for consumption of the whole total. Countdowns are
        measurements, never running totals, and don't pass here. A lower
        value lasting _RUNNING_TOTAL_RESET_S is accepted as a real reset.
        Compared with the divisor applied, so a firmware update that only
        changes a counter's divisor isn't a drop.
        """
        point_id = entity_info["point_id"]
        value = raw_value / (metadata.get("divisor") or 1)
        last = self._running_totals.get(point_id)
        if last is None or value >= last:
            self._running_totals[point_id] = value
            self._running_total_low_since.pop(point_id, None)
            return False
        now = time.time()
        since = self._running_total_low_since.setdefault(point_id, now)
        if now - since < _RUNNING_TOTAL_RESET_S:
            if since == now:
                # pragma: no mutate start
                log_entities.info(
                    "Point %d (%s): running total %s is below its last value %s — "
                    "unavailable until it is back (accepted as a reset after %d min)",
                    point_id,
                    entity_info.get("point_data", {}).get("display_title", ""),
                    value,
                    last,
                    _RUNNING_TOTAL_RESET_S // 60,
                )
                # pragma: no mutate end
            return True
        # pragma: no mutate start
        log_entities.info(
            "Point %d (%s): running total stayed at %s, below %s, for %d min — accepted as a reset",
            point_id,
            entity_info.get("point_data", {}).get("display_title", ""),
            value,
            last,
            _RUNNING_TOTAL_RESET_S // 60,
        )
        # pragma: no mutate end
        self._running_totals[point_id] = value
        del self._running_total_low_since[point_id]
        # Now, not within the hour: a restart would hold the reset again.
        self._save_running_totals()
        return False

    def _load_running_totals(self, path: str | None = None) -> None:
        """Load the last published running totals (_RUNNING_TOTALS_FILE)."""
        try:
            with open(path or _RUNNING_TOTALS_FILE, encoding="utf-8") as f:
                loaded = json.loads(f.read())
            if isinstance(loaded, dict):
                self._running_totals = {int(pid): float(v) for pid, v in loaded.items()}
        except (OSError, ValueError, TypeError):
            pass

    def _save_running_totals(self, path: str | None = None) -> None:
        """Write the last published running totals to disk, at most every
        _RUNNING_TOTALS_SAVE_S: they only go up, so an older copy is still a
        safe reference for a drop to 0, and the flash isn't written each poll."""
        self._running_totals_saved_at = time.time()
        if not self._running_totals:
            return
        payload = json.dumps({str(pid): v for pid, v in sorted(self._running_totals.items())})
        try:
            atomic_write_text(path or _RUNNING_TOTALS_FILE, payload)
        except OSError as e:
            log_entities.warning("Could not write running totals file: %s", e)  # pragma: no mutate

    def _publish_availability(self, entity_info: dict, payload: str) -> None:
        """Send an entity's availability (retained) unless it is what was
        last sent on that topic (_availability_sent)."""
        topic = entity_info.get("availability_topic")
        if not topic or self._availability_sent.get(topic) == payload:
            return
        self.mqtt.publish(topic, payload, retain=True)
        self._availability_sent[topic] = payload

    def _clear_state_once(self, entity_info: dict) -> None:
        """Clear an entity's retained state (an empty payload) once, until a
        valid value is published again (_state_cleared)."""
        point_id = entity_info["point_id"]
        if point_id in self._state_cleared or not entity_info.get("state_topic"):
            return
        self.mqtt.publish(entity_info["state_topic"], "", retain=True)
        self._state_cleared.add(point_id)

    def _process_and_publish_state(
        self,
        entity_info: dict,
        raw_value: int,
        string_value: str,
        metadata: dict,
        force: bool = False,
    ) -> None:
        """Process a raw value and publish the HA state for one entity."""
        point_id = entity_info["point_id"]
        entity_type = entity_info["entity_type"]
        # Sentinel value handling
        sentinel_values = {
            "s8": -128,
            "u8": 255,
            "s16": -32768,
            "u16": 65535,
            "s32": -2147483648,
            "u32": 4294967295,
        }
        variable_size = metadata.get("variableSize", "")  # pragma: no mutate
        if variable_size in sentinel_values and raw_value == sentinel_values[variable_size]:
            # Sentinel means "sensor not connected / no valid value".
            # Publish offline so HA shows the entity as unavailable rather
            # than a misleading zero value regardless of entity type.
            self._publish_availability(entity_info, "offline")
            # Clear any retained state value too, not just availability.
            # Otherwise a value published before this sentinel was ever
            # recognized (an older bridge version, or this point simply
            # flipping from OK to sentinel) sits retained on the broker
            # forever -- HA's MQTT platform re-validates it against the
            # entity's declared bounds on every reconnect/resubscribe (an
            # HA restart, broker restart, etc.) independently of the
            # availability topic, producing a permanent "Invalid value"
            # log entry on every future restart even though the bridge
            # itself stopped publishing anything bad long ago.
            self._clear_state_once(entity_info)
            return

        # A second, register-specific sentinel — scoped to "number" entities
        # only. The firmware's own declared min/max (used to build the HA
        # "number" entity's enforced bounds — see build_number_config in
        # nibe_discovery_config.py) is sometimes violated by the firmware's
        # own current reading — e.g. an unconfigured zone's "desired room
        # temperature" (valid range 5.0-35.0 °C) reporting 0 to mean "not
        # configured" rather than a real target. Passing that straight
        # through as the entity's state made HA's own MQTT number platform
        # reject it every poll ("Invalid value ... range 5.0 - 35.0"),
        # logged repeatedly. Treat it the same as the sentinel case above:
        # unavailable, not a misleading in-range-looking number.
        #
        # Deliberately not extended to other entity types: minValue/maxValue
        # doesn't reliably describe a meaningful bound for all of them (a
        # "time" register's natural range is seconds-since-midnight, far
        # outside whatever small operational min/max its metadata happens to
        # declare), and "select"/"sensor" raw values outside a typical range
        # are still worth showing as their raw fallback rather than hiding.
        #
        # minValue == maxValue is excluded — that's this firmware's own
        # convention for "no fixed bounds declared" on many legitimate
        # read-only sensors (confirmed against point 4, Current outdoor
        # temperature, which reports min=max=0 despite being a real,
        # working, non-zero sensor) — checking those against a degenerate
        # 0-0 "range" would falsely mark almost every such sensor offline.
        if entity_type == "number":
            min_val = metadata.get("minValue")
            max_val = metadata.get("maxValue")
            if (
                min_val is not None
                and max_val is not None
                and min_val != max_val
                and (raw_value < min_val or raw_value > max_val)
            ):
                if raw_value != 0 and point_id not in self._range_warnings_issued:
                    # 0 is this firmware's well-established "unconfigured/
                    # not set" convention (seen throughout this project's
                    # own mapping work) — an out-of-range value that ISN'T
                    # 0 doesn't match any recognised sentinel, so it's worth
                    # flagging rather than silently treated as understood.
                    # Still published unavailable either way: passing it
                    # through would reproduce the original HA-side rejection
                    # this whole check exists to prevent.
                    #
                    # Gated by _range_warnings_issued (once per point_id,
                    # for the life of the process) for the same reason
                    # build_number_config's own warning in
                    # nibe_discovery_config.py is: this runs on every poll,
                    # and an unrecognised out-of-range value tends to stay
                    # out of range, so without this gate the warning would
                    # repeat indefinitely — the exact kind of log spam this
                    # whole fix exists to eliminate, just from our own
                    # logger instead of HA's.
                    # pragma: no mutate start
                    log_entities.warning(
                        "Point %d (%s): current value %s outside firmware range "
                        "%s–%s and not the recognised 0 sentinel — publishing "
                        "unavailable, but this may be worth investigating.",
                        point_id,
                        entity_info.get("point_data", {}).get("display_title", ""),
                        raw_value,
                        min_val,
                        max_val,
                    )
                    # pragma: no mutate end
                    self._range_warnings_issued.add(point_id)
                self._publish_availability(entity_info, "offline")
                # See the storage-limit sentinel branch above for why the
                # retained state value is cleared too, not just
                # availability -- otherwise a stale "0" published before an
                # HA restart keeps re-triggering "Invalid value ... (range
                # X - Y)" on every future restart, indefinitely, even
                # though the bridge stopped publishing it long ago.
                self._clear_state_once(entity_info)
                return

        if entity_info.get("is_running_total") and self._running_total_dropped(
            entity_info, raw_value, metadata
        ):
            # Home Assistant takes any decrease of a total_increasing sensor
            # for a counter reset and counts the whole total again; it skips
            # an unavailable one. The retained state keeps the last value.
            self._publish_availability(entity_info, "offline")
            return

        if entity_type == "binary_sensor" and raw_value not in (0, 1):
            # Static firmware metadata cannot distinguish a genuine boolean
            # flag from a multi-state enum masquerading as one (see
            # _BINARY_SENSOR_EXCLUSIONS in nibe_entity_detection.py — every
            # metadata field was found byte-identical between confirmed
            # booleans and confirmed disguised enums in a real firmware
            # dump). This is the dynamic safety net for cases not yet known
            # about: reassigning entity_type here (not just entity_info's
            # copy) lets this same poll fall through into the "sensor"
            # dispatch below instead of duplicating its value formatting.
            entity_type = self._reclassify_binary_sensor(point_id, entity_info, raw_value)

        if entity_type == "text":
            state_value = string_value
        elif entity_type == "switch":
            state_value = "1" if raw_value else "0"
        elif entity_type == "binary_sensor":
            state_value = "OFF" if raw_value == 0 else "ON"
        elif entity_type == "time":
            # Convert seconds-since-midnight to HH:MM:SS for HA time entity.
            # Firmware stores time registers as integer seconds; the
            # controller display shows HH:MM. We always emit :00 seconds.
            try:
                secs = int(raw_value) % 86400
                state_value = f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}:00"
            except (ValueError, TypeError):
                state_value = str(raw_value)
        elif entity_type == "sensor" and point_id == 2685:
            # Next periodic increase date — stored as days since 2010-01-01.
            # Convert to ISO date string for HA device_class=date sensor.
            try:
                d = _date(2010, 1, 1) + _timedelta(days=int(raw_value))
                state_value = d.isoformat()  # YYYY-MM-DD
            except (ValueError, OverflowError):
                state_value = str(raw_value)
        elif entity_type == "sensor" and point_id in (2453, 2509):
            state_value = _format_version(point_id, raw_value)
        elif entity_type == "sensor" and point_id == 2022:
            # Current status bitfield — community-decoded from SMO S40 register 31121.
            # Mode bits (high): 14=HW boost, 13=Hot water, 12=Heating, 20=Cooling
            # State bits (low): 4=Compressor starting, 2=Compressor running, 3=Pump running
            try:
                v = int(raw_value)
                _MODE_BITS = {
                    20: "Cooling",
                    14: "Hot water boost",
                    13: "Hot water",
                    12: "Heating",
                }
                # Compressor state: bit4=starting, bit2+4=running, neither=preheating/pump only
                #
                # Each label is translated individually (falling back to its
                # own English word, same as every other VALUE_MAPPINGS-driven
                # state elsewhere in this method) before composing the final
                # string -- this point used to assign raw English literals
                # straight to state_value, bypassing self._value_translations
                # entirely, unlike every other sensor/select value in this
                # function. Translating the composed sentence as one unit
                # isn't an option instead: the translation table is keyed by
                # single English words/phrases, not by the "X + Y (Z)"
                # sentences this composes, so composing first would leave it
                # permanently untranslatable regardless of language.
                modes = [
                    self._value_translations.get(label, label)
                    for bit, label in _MODE_BITS.items()
                    if v & (1 << bit)
                ]
                if modes:
                    v_bit2 = bool(v & (1 << 2))
                    v_bit4 = bool(v & (1 << 4))
                    if v_bit2 and v_bit4:
                        comp_state = self._value_translations.get("Running", "Running")
                    elif v_bit4:
                        comp_state = self._value_translations.get("Starting", "Starting")
                    else:  # a mode active but no compressor bits
                        comp_state = self._value_translations.get("Preheating", "Preheating")
                    state_value = f"{' + '.join(modes)} ({comp_state})"
                else:
                    state_value = self._value_translations.get("Idle", "Idle")
            except (ValueError, TypeError):
                state_value = str(raw_value)
        elif entity_type == "select":
            mapping = entity_info.get("value_mapping")
            if mapping is None:
                mapping = get_value_mapping(point_id, entity_info["point_data"])
                if mapping is not None:
                    # Cache miss — populate for future polls
                    entity_info["value_mapping"] = mapping
            if mapping and raw_value in mapping:
                label = mapping[raw_value]
                state_value = self._value_translations.get(label, label)
            elif mapping:
                # Not one of the select's options: HA rejects such a state
                # (logging a warning every poll) and the entity just shows its
                # last value. Unavailable is the honest answer, as for an
                # out-of-range number; the next valid value brings it back.
                if point_id not in self._range_warnings_issued:
                    # pragma: no mutate start
                    log_entities.warning(
                        "Point %d (%s): value %s is not one of its select options — "
                        "publishing unavailable",
                        point_id,
                        entity_info.get("point_data", {}).get("display_title", ""),
                        raw_value,
                    )
                    # pragma: no mutate end
                    self._range_warnings_issued.add(point_id)
                self._publish_availability(entity_info, "offline")
                return
            else:
                state_value = str(raw_value)
        elif entity_type == "sensor":
            mapping = entity_info.get("value_mapping")
            if mapping is None:
                mapping = get_value_mapping(point_id, entity_info["point_data"])
                if mapping is not None:
                    # Cache miss — populate for future polls
                    entity_info["value_mapping"] = mapping
            if mapping and raw_value in mapping:
                label = mapping[raw_value]
                state_value = self._value_translations.get(label, label)
            else:
                divisor = metadata.get("divisor", 1)  # pragma: no mutate
                state_value = apply_divisor(raw_value, divisor)
        else:
            divisor = metadata.get("divisor", 1)  # pragma: no mutate
            state_value = apply_divisor(raw_value, divisor)

        # A valid value: available. Sent here, once the sentinel, range and
        # option checks above have passed — sending "online" first and
        # "offline" after made every such entity flip on every poll.
        self._publish_availability(entity_info, "online")
        self._state_cleared.discard(point_id)

        # A setting's tile on the Nibe Menus dashboard shows live whether it
        # differs from its factory default; the publisher republishes the
        # attribute only when that flips.
        if entity_info.get("entity_type") in ("number", "select", "switch"):
            default = meaningful_default(metadata, point_id, entity_info.get("value_mapping"))
            if default is not None:
                self._pub.update_default_status(point_id, raw_value != default)

        # Every real change is published, however small. The firmware's
        # 'change' metadata is not a publish threshold: it is a setting's
        # step (build_number_config). A measurement's resolution is its
        # divisor — the controller shows the THS-10 at 20.8 while its change
        # is 5.
        if (
            force
            or point_id not in self._state_sent
            or self.last_states.get(point_id) != state_value
        ):
            if not entity_info.get("state_topic"):
                if point_id not in self._missing_state_topic_warned:
                    # pragma: no mutate start
                    log_entities.warning(
                        "Point %d (%s): no state_topic — cannot publish state",
                        point_id,
                        entity_info.get("entity_type"),
                    )
                    # pragma: no mutate end
                    self._missing_state_topic_warned.add(point_id)
                return
            self.mqtt.publish(entity_info["state_topic"], state_value, retain=True)
            with self._em_lock:
                self.last_states[point_id] = state_value
                self._state_sent.add(point_id)

    def _reclassify_binary_sensor(self, point_id: int, entity_info: dict, raw_value: int) -> str:
        """Reclassify a binary_sensor point to sensor after observing a raw
        value other than 0/1, and return the new entity_type ("sensor").

        Static firmware metadata cannot reliably distinguish a genuine
        boolean flag from a multi-state enum masquerading as one — see
        _BINARY_SENSOR_EXCLUSIONS in nibe_entity_detection.py, whose
        docstring records that every metadata field was found byte-identical
        between confirmed-boolean and confirmed-disguised-enum points in a
        real firmware dump (GitHub issue #35 and related reports). That
        static list only ever fixes points already reported; this is the
        dynamic, forward-looking counterpart for firmware/hardware we
        haven't seen yet — it reclassifies on the first observed violation
        rather than waiting for a bug report.

        Deliberately loud, not silent: this is a one-time, user-visible
        correction (renames the HA entity binary_sensor.nibe_<id> ->
        sensor.nibe_<id> and resets its history — an accepted, unavoidable
        cost, same as the static fix), so it must never look like an
        ambient, unlogged behavior change.

        Three things happen, in order:
          1. Warn at WARNING level — but only once per point_id (tracked in
             _binary_sensor_reclassified). This is the reclassifying
             transition itself; every subsequent poll of an already-
             reclassified point is expected (not surprising) behavior, and
             warning again on every single poll thereafter would just be
             log noise for a condition that is no longer misclassified.
             publish_entity_discovery's own config_hash / entity_type-change
             bookkeeping (see nibe_mqtt_publisher.py) independently already
             prevents any duplicate MQTT republish on repeat polls, so this
             per-point warning gate mirrors, rather than papers over, that
             existing behavior.
          2. Update point_data["entity_type"] (the actual dict referenced by
             both all_points_by_id and this entity_info's "point_data" key —
             not a copy) and the _entity_type_cache, so _get_cached_entity_type
             doesn't hand back the stale "binary_sensor" verdict on the next
             lookup and flip this point back.
          3. Re-publish MQTT discovery under the new domain. publish_entity_discovery
             compares the new entity_type against its own last-published-type
             and retained-domain bookkeeping and automatically clears the old
             binary_sensor discovery topic — see that function's stale_domains
             handling. entity_info is mutated in place (not reassigned) so the
             caller's existing reference — including the one held in
             active_entities_by_id — picks up the new state_topic/entity_type
             immediately, this same poll.
        """
        if point_id not in self._binary_sensor_reclassified:
            self._binary_sensor_reclassified.add(point_id)
            self._persist_reclassified_points()
            # pragma: no mutate start
            title = entity_info["point_data"].get("display_title") or f"Point {point_id}"
            log_entities.warning(
                "Point %d (%s) classified as binary_sensor observed value %r, "
                "expected 0/1 — reclassifying as sensor. This may indicate an "
                "undocumented multi-state register; please consider filing a "
                "GitHub issue with this point's ID and raw value.",
                point_id,
                title,
                raw_value,
            )
            # pragma: no mutate end

        point = entity_info["point_data"]
        type_changed = point.get("entity_type") != "sensor"
        # Move this point's stats count from its old type to "sensor" —
        # changing entity_type alone left it counted under binary_sensor,
        # and its eventual disable would then decrement sensor instead.
        # Guarded so a retry after a failed republish (point already
        # "sensor", entity_info not yet updated) doesn't move it twice.
        if type_changed and point_id in self.mqtt_enabled_points:
            self._decrement_stats(point)
            point["entity_type"] = "sensor"
            self._increment_stats(point)
        point["entity_type"] = "sensor"
        # point_data is the dict the entity was enabled with. A poll
        # without change detection re-indexes every point into a fresh
        # dict, so all_points_by_id may hold another one — still
        # binary_sensor for the rest of this poll, which is exactly what
        # the point catalog published below would have told the card.
        indexed = self.all_points_by_id.get(point_id)
        if indexed is not None and indexed is not point:
            indexed["entity_type"] = "sensor"
        # category is left as whatever detect_entity_type() originally
        # assigned — we have no more information about the correct
        # category than we did before; only entity_type is known-wrong.
        self._entity_type_cache.put(point_id, ("sensor", point["entity_category"]))
        new_entity_info = self._pub.publish_entity_discovery(point, self.bulk_data)
        if new_entity_info:
            entity_info.clear()
            entity_info.update(new_entity_info)
        # Without this the card kept listing the point as a binary_sensor
        # until the next restart: meta/{id} updates an open card, and the
        # catalog keeps a freshly loaded one from applying the stale
        # discovery-time all_metadata over it.
        if type_changed:
            self._pub.publish_point_metadata(point)
            self._point_catalog_changed()
        return "sensor"

    # ------------------------------------------------------------------ #
    # Bulk data fetch                                                      #
    # ------------------------------------------------------------------ #

    def _attributable_poll(self, response: dict, own_point: int | None = None) -> set[int] | None:
        """The point ids ``response`` serves, when the points appearing in it
        may be linked to a switch changed before it — else None.

        ``bulk_data`` and ``published_configs`` still hold the previous poll
        when this runs. Not before initial discovery, nor with an HA write
        pending (other than one to ``own_point``) or queued: that write opens
        its own window. A previously served point missing — other than a known
        dynamic one — or a changed firmware version register means a
        restarting or updating controller, whose new points are the
        firmware's: no point is linked for _ABSENT_GRACE_S, and a late-arrival
        period is dropped. A failed poll does the same (_handle_api_failure).
        """
        with self._pending_writes_lock:
            other_pending = bool(set(self.pending_writes) - {own_point})
        with self._write_queue_lock:
            other_pending = other_pending or bool(self._write_queue_depth)
        served: set[int] = set()
        for key in response:
            try:
                served.add(int(key))
            except (TypeError, ValueError):
                continue
        with self._em_lock:
            known_dynamic = self.dynamic_point_map.all_known_dynamic_point_ids()
        now = time.time()
        verdict = poll_verdict(
            discovery_complete=self.initial_discovery_complete,
            published=self.published_configs,
            now=now,
            blocked_until=self._attribution_blocked_until,
            other_write_pending=other_pending,
            served=served,
            known_dynamic=known_dynamic,
            versions_changed=version_changed(
                {
                    pid: self.bulk_data[pid].get("raw_value")
                    for pid in _FIRMWARE_VERSION_POINTS
                    if pid in self.bulk_data
                },
                _response_values(response),
                _FIRMWARE_VERSION_POINTS,
            ),
        )
        if verdict == "block":
            self._attribution_blocked_until = now + _ABSENT_GRACE_S
            self._late_tail = None
        return served if verdict == "ok" else None

    def _changed_candidates(self, response: dict) -> list[tuple[int, int, int, DynamicPointEntry]]:
        """The candidate switches and selects whose value differs between the
        previous poll (``bulk_data``) and ``response``, as
        ``(point_id, old_value, new_value, entry)``."""
        with self._em_lock:
            entries = list(self.dynamic_point_map.items())
        changed = changed_candidates(
            ((pid, entry.firmware_removed) for pid, entry in entries),
            {pid: data.get("raw_value") for pid, data in self.bulk_data.items()},
            _response_values(response),
        )
        by_id = dict(entries)
        return [(pid, old, new, by_id[pid]) for pid, old, new in changed]

    def _controller_side_change(self, response: dict) -> tuple[int, int, int] | None:
        """The one switch or select changed on the controller itself since the
        previous poll, as ``(point_id, old_value, new_value)``, when that
        change should open a scan window — else None.

        Points a switch shows are learned from the scan window an HA write to
        it opens. A switch flipped on the display opened none, so the first
        time the firmware served the points it shows they were indexed as
        static. ``response`` is the new bulk response; ``bulk_data`` still
        holds the previous poll's values when this runs. The guards keep a
        wrong link out — a point wrongly recorded as dynamic is disabled the
        moment it is briefly absent, losing its history:

        - the poll must be attributable (_attributable_poll): no HA write
          pending or queued, and no sign of a restarting or updating
          controller;
        - more than one candidate changed: which one shows what can't be told;
        - the new value already learned (the known-dynamic path handles it),
          or this (point, value) already opened a window in this run. Points
          such a change shows that weren't known yet are still linked to it
          when they arrive (_late_arrival).
        """
        if self._attributable_poll(response) is None:
            return None
        changed = self._changed_candidates(response)
        decision = controller_side_decision(
            [(pid, old, new) for pid, old, new, _entry in changed],
            {pid: entry.unprocessed_values for pid, _old, _new, entry in changed},
            self._controller_side_tried,
        )
        if decision is None:
            return None
        if decision.open_window:
            self._controller_side_tried.add((decision.point_id, decision.new))
            return decision.point_id, decision.old, decision.new
        self._start_late_tail(decision.point_id, decision.new, True, after=_POST_WRITE_SCAN_S)
        return None

    def _start_late_tail(
        self, point_id: int, value: int, controller_side: bool, after: float
    ) -> None:
        """Link new points to ``point_id`` at ``value`` for ``after`` seconds
        plus the late-arrival period (_late_arrival)."""
        until = late_tail_until(time.time(), after, _LATE_ARRIVAL_S, self.bulk_interval)
        self._late_tail = LateTail(point_id, value, until, controller_side)

    def _late_arrival(self, response: dict) -> tuple[int, int, bool, list[int]] | None:
        """New points that arrive after a switch's scan window ended, as
        ``(point_id, value, changed_on_controller, new_point_ids)`` — else None.

        The firmware serves the points a switch shows about a minute after it
        changes, but not always within the 90 s window: an HA write to a
        switch not learned yet then recorded "shows nothing" for both its
        values, after which it was never scanned again, and its points were
        indexed as static whenever they came. So a window that ends leaves a
        late-arrival period (_start_late_tail): points that are new — not
        served before, not indexed, not known dynamic — in an attributable
        poll during it are linked to that switch. A write to a switch learned
        as showing nothing, or a learned value set on the controller, opens no
        window but leaves the same period, so a switch learned wrongly before
        is corrected the next time its points arrive. The period ends at its
        first use, when the switch no longer has that value, or when another
        switch or select changes.
        """
        tail = self._late_tail
        if tail is None:
            return None
        point_id, value, until = tail.point, tail.value, tail.until
        now = time.time()
        # Expiry first: an expired period must not run _attributable_poll,
        # whose verdict can block attribution.
        served = self._attributable_poll(response, own_point=point_id) if now <= until else None
        if self._late_tail is None:
            return None  # _attributable_poll blocked attribution and dropped it
        with self._em_lock:
            known_dynamic = self.dynamic_point_map.all_known_dynamic_point_ids()
        decision = late_arrival_decision(
            until=until,
            value=value,
            now=now,
            served=served,
            current_value=_response_values(response).get(point_id),
            others_changed=any(c[0] != point_id for c in self._changed_candidates(response)),
            published=self.published_configs,
            baseline=self.baseline_point_ids,
            known_dynamic=known_dynamic,
        )
        if not decision.keep_tail:
            self._late_tail = None
        if not decision.new_points:
            return None
        return point_id, value, tail.controller_side, decision.new_points

    def _other_switch_spoils_window(self, response: dict, window: ScanWindow) -> ScanWindow:
        """Make ``window`` record nothing when another switch or select
        changed on the controller while it is open — the settings that one
        shows would otherwise be recorded under ``window``'s point.

        ``response`` is the new bulk response; ``bulk_data`` still holds the
        previous poll. A switch learned as showing nothing is left out: it
        can't explain points appearing. The window stays open, only as
        ambiguous (value None, as for two values of its own switch), and a
        value being learned stays to be learned on a later change.
        """
        others = sorted(
            pid
            for pid, _old, _new, entry in self._changed_candidates(response)
            if pid != window.point and not (entry.is_fully_processed() and not entry.is_controlling)
        )
        if not others:
            return window
        with self._post_write_lock:
            current = self._window
            if current is None or current.point != window.point:
                return window
            if current.learning_value is not None:
                self._learning_window_ended.set()
            self._window = dataclasses.replace(current, value=None, learning_value=None)
            spoiled = self._window
        # pragma: no mutate start
        log_discovery.info(
            "Point(s) %s changed on the controller during the scan window of point %d — "
            "nothing that appears in it is recorded as what either shows",
            others,
            window.point,
        )
        # pragma: no mutate end
        return spoiled

    def _open_learning_window(
        self, point_id: int, controller_side: bool, value: int
    ) -> ScanWindow | None:
        """Open a scan window for ``point_id`` at ``value`` from the poll loop
        and return it, unless one is open — an HA write's window keeps its
        own point."""
        with self._post_write_lock:
            if self._window is not None:
                return None
            self._window = ScanWindow(
                point=point_id,
                until=time.time() + self._post_write_duration,
                controller_side=controller_side,
                value=value,
            )
            return self._window

    def _fetch_bulk_data(
        self,
        detect_changes: bool = True,
    ) -> bool | int:
        """Fetch bulk data from the API and process all points.

        Protected by _bulk_fetch_lock so concurrent callers (e.g. a force
        poll and the normal polling loop) never run simultaneously.

        Returns:
          True   — success (whether or not dynamic changes were detected —
                   callers only test truthiness/is False, not a count)
          False  — failure or lock busy
        """
        if not self._bulk_fetch_lock.acquire(blocking=False):
            # pragma: no mutate start
            log_discovery.info(  # pragma: no mutate
                "Bulk fetch skipped — a previous fetch is still running "
                "(API may be slow; consider raising poll_interval)"
            )
            # pragma: no mutate end
            return False
        try:
            start_time = time.time()
            response = self._api.fetch_bulk_points()

            if not response or not isinstance(response, dict):
                # Captured immediately after the (successful, no exception)
                # fetch_bulk_points() call and before the logging below —
                # same reasoning as the HTTPError/Exception branches further
                # down: self._api is shared with the write-executor thread,
                # and log_discovery.warning() can yield the GIL, so a fresh
                # read inside _handle_api_failure's own fallback could pick
                # up an unrelated concurrent write's last_error instead of
                # this call's (expected-None, since the fetch succeeded).
                api_last_error = self._api.last_error  # pragma: no mutate
                # pragma: no mutate start
                log_discovery.warning(
                    "Bulk fetch returned an unexpected response type (%s) — "
                    "expected a JSON object. API may be temporarily unavailable.",
                    type(response).__name__,
                )
                # pragma: no mutate end
                self._handle_api_failure(api_last_error)
                return False

            current_point_ids = set()
            new_points = []
            # Points re-indexed by the non-detect branch below after initial
            # discovery (e.g. one deindexed by the absence-grace disable that
            # has since come back) — announced to the card after the loop.
            reindexed_points: list[int] = []
            # Read the window once, for the whole fetch cycle: a write can
            # replace it while this cycle is still running (a write to a
            # switch whose values are all learned doesn't wait for its
            # window), and re-reading it later — e.g. when
            # _publish_dynamic_changes attributes newly-appeared points — would
            # credit this cycle's changes to that *other* write's point.
            with self._post_write_lock:
                window_snapshot = self._window
            if window_snapshot is None:
                # Decided before the point loop below, which would otherwise
                # classify points arriving in this same poll first.
                window_snapshot = self._window_opened_by_poll(response)
                if window_snapshot is not None:
                    detect_changes = True
            elif window_snapshot.value is not None:
                window_snapshot = self._other_switch_spoils_window(response, window_snapshot)
            post_write_snapshot = window_snapshot is not None
            # pragma: no mutate start
            if post_write_snapshot:
                scan_type = "post-write-scan"
            elif detect_changes:
                known_count = len(self.dynamic_point_map.all_known_dynamic_point_ids())
                scan_type = f"dynamic-monitoring({known_count} known)"
            else:
                scan_type = "poll"
            log_discovery.debug(
                "Processing bulk response: %d points (%s)",
                len(response),
                scan_type,
            )
            # pragma: no mutate end

            now = time.time()

            for point_id_str, point_data in response.items():
                try:
                    point_id = int(point_id_str)
                    current_point_ids.add(point_id)

                    metadata, title, description = self._update_bulk_entry(
                        point_id, point_data, now
                    )

                    if (
                        detect_changes
                        and self.initial_discovery_complete
                        and point_id not in self.baseline_point_ids
                        and point_id not in self.published_configs
                        and self._is_dynamic_appearance(
                            point_id, metadata, title, description, post_write_snapshot
                        )
                    ):
                        new_points.append((point_id, point_data))

                    if not detect_changes and not self.dynamic_point_map.is_known_dynamic(point_id):
                        if (
                            self.initial_discovery_complete
                            and point_id not in self.all_points_by_id
                        ):
                            reindexed_points.append(point_id)
                        self._index_static_point(point_id, metadata, title, description)

                except (ValueError, KeyError) as e:
                    # pragma: no mutate start
                    log_discovery.warning("Error processing point %s: %s", point_id_str, e)
                    # pragma: no mutate end
                    continue

            # ── Remove points that disappeared from the API response ───────────
            # (Finding 4: maintain bulk_data in-place rather than full rebuild)
            # gone_id always exists in both — pop()'s default is unreachable
            # (bulk_data.keys() is where gone_id came from; LRUCache.pop's
            # own signature already defaults to None regardless).
            for gone_id in set(self.bulk_data.keys()) - current_point_ids:
                self.bulk_data.pop(gone_id, None)  # pragma: no mutate
                self._point_string_cache.pop(gone_id)

            disappeared_points = (
                self._dynamic_disappearances(current_point_ids)
                if detect_changes and self.initial_discovery_complete
                else set()
            )

            # The detect_changes path announces a returning point itself
            # (_is_dynamic_appearance, for a new permanent point); this one used to re-index silently, so a point
            # deindexed by the absence-grace disable came back enabled (via
            # _reconcile_wanted_points) but never reappeared in the card.
            if reindexed_points and self._pub:
                for pid in reindexed_points:
                    self._pub.publish_point_metadata(self.all_points_by_id[pid])
                self._point_catalog_changed()

            point_set_changed = current_point_ids != self.published_configs
            self.published_configs = current_point_ids

            # Not only when detecting: a write can open a window after the
            # poll loop decided not to, and a point-set change still releases
            # the write waiting for it. Both lists are empty then.
            self._handle_dynamic_changes(
                window_snapshot, new_points, disappeared_points, point_set_changed
            )

            elapsed = time.time() - start_time
            self.last_fetch_duration = elapsed

            self._note_api_success()

            # pragma: no mutate start
            log_discovery.debug("Processed %d points in %.2fs", len(current_point_ids), elapsed)
            # pragma: no mutate end
            # Success is True regardless of whether anything changed, as the
            # docstring says. This used to return
            # `bool(new_points or disappeared_points)` when detect_changes was
            # set, so a quiet poll returned False — which update_all_states
            # reads as "the fetch lock was busy" and then skipped
            # _reconcile_wanted_points. With any dynamic point known (the
            # normal state once anything has been learned), the wanted-points
            # safety net only ever ran on polls that also saw a dynamic change.
            return True

        except urllib.error.HTTPError as e:
            # Captured immediately, before any logging that could yield the
            # GIL to the write-executor thread — see _handle_api_failure's
            # docstring for why this can't be re-read later.
            api_last_error = self._api.last_error
            if e.code in (401, 403):
                # pragma: no mutate start
                log_discovery.warning(
                    "Bulk fetch rejected (HTTP %d) — credentials may not yet be "
                    "configured on the controller. Will retry.",
                    e.code,
                )
                # pragma: no mutate end
            else:
                # pragma: no mutate start
                log_discovery.warning(
                    "Bulk fetch failed with HTTP %d — will retry.",
                    e.code,
                )
                # pragma: no mutate end
            self._handle_api_failure(api_last_error)
            return False
        except Exception:
            # Captured before the logging below, as in the HTTPError branch.
            api_last_error = self._api.last_error
            # pragma: no mutate start
            log_discovery.exception(
                "Unhandled exception during bulk fetch — this is a bug, please report it"
            )
            # pragma: no mutate end
            self._handle_api_failure(api_last_error)
            return False
        finally:
            self._bulk_fetch_lock.release()

    def _update_bulk_entry(
        self, point_id: int, point_data: dict, now: float
    ) -> tuple[dict, str | None, str | None]:
        """Update bulk_data for one point of a bulk response; returns its
        metadata (overrides applied), cleaned title and description."""
        # `or {}` (not just a .get default) also covers the API
        # sending an explicit "value": null / "metadata": null —
        # .get()'s default only applies when the key is absent,
        # so a present-but-null value would otherwise reach the
        # .get() calls below as None and raise AttributeError,
        # which isn't caught by _fetch_bulk_data's (ValueError, KeyError)
        # and would abort the whole bulk fetch for every point.
        value_data = point_data.get("value") or {}
        metadata = apply_metadata_overrides(point_id, point_data.get("metadata") or {})

        # ── String cache (Finding 2) ──────────────────────────────
        # title and description never change between firmware updates.
        # Cache the clean_string result; only recompute when the raw
        # string from the API actually differs.
        raw_title = point_data.get("title", f"Point {point_id}")
        raw_desc = point_data.get("description", "")
        cached_strings = self._point_string_cache.get(point_id)
        if (
            cached_strings is None
            or cached_strings[0] != raw_title
            or cached_strings[1] != raw_desc
        ):
            title = clean_string(raw_title)
            description = clean_string(raw_desc)
            self._point_string_cache.put(point_id, (raw_title, raw_desc, title, description))
        else:
            title, description = cached_strings[2], cached_strings[3]

        # ── In-place bulk_data update (Finding 4) ─────────────────
        # Update the existing dict entry rather than building a new
        # one each poll.  Metadata, title, and description are only
        # updated when they differ (which is essentially never on a
        # running installation) to avoid redundant object churn.
        existing = self.bulk_data.get(point_id)
        if existing is None:
            self.bulk_data[point_id] = {
                "raw_value": value_data.get("integerValue", 0),
                "string_value": value_data.get("stringValue", ""),
                "is_ok": value_data.get("isOk", False),
                "metadata": metadata,
                "title": title,
                "description": description,
                "timestamp": now,
            }
        else:
            existing["raw_value"] = value_data.get("integerValue", 0)
            existing["string_value"] = value_data.get("stringValue", "")
            existing["is_ok"] = value_data.get("isOk", False)
            existing["timestamp"] = now
            existing["title"] = title
            existing["description"] = description
            # Replaced only when its content changed: the firmware check
            # (_check_firmware_update) watches this object's identity.
            if existing.get("metadata") != metadata:
                existing["metadata"] = metadata
        return metadata, title, description

    def _index_static_point(
        self, point_id: int, metadata: dict, title: str | None, description: str | None
    ) -> str:
        """Classify and index a point as static; returns its entity type."""
        point = self._classify_and_index(point_id, metadata, title, description, False)
        entity_type: str = point["entity_type"]
        return entity_type

    def _classify_and_index(
        self,
        point_id: int,
        metadata: dict,
        title: str | None,
        description: str | None,
        is_dynamic: bool,
    ) -> dict:
        """Classify a point and index it in all_points_by_id; returns the
        indexed point."""
        # The title only reaches detect_entity_type's override log line.
        entity_type, category = self._get_cached_entity_type(
            {
                "variableId": point_id,
                "metadata": metadata,
                "title": title,
                "description": description,
            }
        )
        point = {
            "variableId": point_id,
            "display_title": title,
            "description": description,
            "metadata": metadata,
            "entity_type": entity_type,
            "entity_category": category,
            "is_writable": is_writable_point(metadata, point_id),
            "is_dynamic": is_dynamic,
        }
        self._index_point(point)
        return point

    def _is_dynamic_appearance(
        self,
        point_id: int,
        metadata: dict,
        title: str | None,
        description: str | None,
        in_window: bool,
    ) -> bool:
        """Decide what a point new since the last poll is.

        True: a dynamic appearance, for _publish_dynamic_changes — every
        point appearing during a scan window, and a known dynamic point
        outside one (a bulk fetch catching its appearance after the window
        expired; routing it as static would permanently misclassify it).
        False: a genuinely unknown point outside a window, a permanent
        firmware addition (e.g. a firmware update), indexed as static here.
        """
        if in_window:
            return True
        if self.dynamic_point_map.is_known_dynamic(point_id):
            # pragma: no mutate start
            log_discovery.info(
                "Known dynamic point %d '%s' appeared outside scan window — routing as dynamic",
                point_id,
                title,
            )
            # pragma: no mutate end
            return True
        # pragma: no mutate start
        log_discovery.info(
            "New permanent point %d '%s' appeared during normal poll — indexing as static",
            point_id,
            title,
        )
        # pragma: no mutate end
        entity_type = self._index_static_point(point_id, metadata, title, description)
        self.baseline_point_ids.add(point_id)
        if entity_type in ("switch", "select"):
            # dynamic_point_map._table is also mutated by a Force Poll or
            # Flush Dynamic Map on the management thread — _em_lock
            # serializes all of them.
            with self._em_lock:
                self.dynamic_point_map.populate_from_bulk(
                    {point_id: self.all_points_by_id[point_id]}, {point_id: entity_type}
                )
        # _index_static_point (above) indexed it.
        self._pub.publish_point_metadata(self.all_points_by_id[point_id])
        self._point_catalog_changed()
        return False

    def _dynamic_disappearances(self, current_point_ids: set[int]) -> set[int]:
        """Active known dynamic points missing from this bulk response.

        A baseline point that isn't a known dynamic point is not taken for a
        dynamic disappearance just because a write was made in the last 90s:
        it may be a controller restart or a partial response, so it gets the
        same absence grace period as at any other time (_update_entity_state).
        """
        known_dynamic_ids = self.dynamic_point_map.all_known_dynamic_point_ids()
        disappeared = {
            pid
            for pid in known_dynamic_ids & self.active_dynamic_points
            if pid not in current_point_ids
        }
        if disappeared:
            # pragma: no mutate start
            log_discovery.debug(
                "Dynamic points absent from this fetch: %s",
                sorted(disappeared),
            )
            # pragma: no mutate end
        return disappeared

    def _handle_dynamic_changes(
        self,
        window: ScanWindow | None,
        new_points: list[tuple[int, dict]],
        disappeared_points: set[int],
        point_set_changed: bool,
    ) -> None:
        """Act on the dynamic changes of one detecting poll, judged in the
        scan window read at its start (``window``)."""
        if new_points and window is not None:
            # A learning window that saw points must not end by recording
            # "shows nothing" (_finish_learning_window). Marked on the
            # window still open for the same point, if any.
            with self._post_write_lock:
                if self._window is not None and self._window.point == window.point:
                    self._window = dataclasses.replace(self._window, saw_points=True)
        if new_points or disappeared_points:
            # pragma: no mutate start
            log_discovery.debug(
                "Dynamic changes detected: +%d new, -%d disappeared",
                len(new_points),
                len(disappeared_points),
            )
            # pragma: no mutate end
            self._publish_dynamic_changes(
                new_points,
                disappeared_points,
                window.point if window else None,
                controller_side=bool(window and window.controller_side),
                controlling_value=window.value if window else None,
            )
        if window is not None:
            if point_set_changed:
                self._release_learning_write()
            else:
                # pragma: no mutate start
                log_discovery.debug(
                    "Post-write scan: no dynamic changes yet (known=%d)",
                    len(self.dynamic_point_map.all_known_dynamic_point_ids()),
                )
                # pragma: no mutate end

    def _note_api_success(self) -> None:
        """Clear the API-failure bookkeeping and notifications after a
        successful bulk fetch."""
        # Both notifications promise to clear once the controller answers,
        # but whether one is showing is only known to the process that
        # raised it. After a restart while the controller was unreachable —
        # often the user's own troubleshooting step — they stayed in HA
        # for good. Dismissed unconditionally on this process's first
        # success, like the write-error notification at startup.
        if self._api_first_success_pending:
            self._api_first_success_pending = False
            if self.mqtt and not self._api_notification_active:
                self._dismiss(self.mqtt, _NOTIF_API_UNREACHABLE)
            if self.mqtt and not self._discovery_notification_active:
                self._dismiss(self.mqtt, _NOTIF_DISCOVERY_INCOMPLETE)

        if (
            self.api_consecutive_failures >= self.api_failure_threshold
            and self._api_notification_active
            and self.mqtt
        ):
            self._dismiss(self.mqtt, _NOTIF_API_UNREACHABLE)
            self._api_notification_active = False
            # Publish a structured resolution alert so automations know
            # the API outage has cleared — mirrors the "api_unreachable"
            # alert published in _handle_api_failure.
            if self._pub:
                self._pub.publish_bridge_alert(
                    alert_type="api_restored",
                    severity="info",
                    message=(
                        f"Nibe API contact restored after "
                        f"{self.api_consecutive_failures} failed polls."
                    ),
                    context={
                        "previous_failure_count": self.api_consecutive_failures,
                        "api_url": self._api.base_url,
                    },
                )

        if self._discovery_notification_active and self.mqtt:
            self._dismiss(self.mqtt, _NOTIF_DISCOVERY_INCOMPLETE)
            self._discovery_notification_active = False

        self.api_consecutive_failures = 0
        self.api_last_success_time = time.time()

    def _window_opened_by_poll(self, response: dict) -> ScanWindow | None:
        """Open a scan window for what this poll shows, outside any window.

        A switch or select changed on the controller itself gets the same
        scan window an HA write would, so the points it shows — which the
        firmware serves about a minute later — are learned as its dynamic
        points instead of indexed as static ones. Points arriving after a
        window ended are linked the same way (_late_arrival). Returns the
        window opened, or None.
        """
        changed = self._controller_side_change(response)
        if changed is not None:
            window = self._open_learning_window(changed[0], True, changed[2])
            # pragma: no mutate start
            if window is not None:
                log_discovery.info(
                    "Point %d changed on the controller (%s -> %s) — scan window "
                    "opened for up to %ds to learn the points it shows",
                    changed[0],
                    changed[1],
                    changed[2],
                    self._post_write_duration,
                )
            # pragma: no mutate end
            return window
        late = self._late_arrival(response)
        if late is None:
            return None
        window = self._open_learning_window(late[0], late[2], late[1])
        # pragma: no mutate start
        if window is not None:
            log_discovery.info(
                "New point(s) %s arrived after the scan window for point %d "
                "(value %d) — learning them as points it shows",
                late[3],
                late[0],
                late[1],
            )
        # pragma: no mutate end
        return window

    def _handle_api_failure(self, last_error: str | None = None) -> None:
        """Record a bulk-fetch failure and notify HA once the threshold is hit.

        Prefer passing *last_error*, captured by the caller at the moment its
        own request failed (``self._api.last_error`` read immediately in the
        except block) — ``self._api`` is shared with
        the write-command executor thread, which can run its own
        request()/last_error assignment between this failure and whenever
        this method happens to run, so re-reading ``self._api.last_error``
        here risks attributing the wrong request's error (or masking a real
        one with a stale success) to this notification. When omitted, falls
        back to reading it fresh, for callers with no closer capture point.
        """
        self.api_consecutive_failures += 1
        # A controller that doesn't answer may be restarting or updating:
        # points it serves next are its own, not a switch's (_attributable_poll).
        self._attribution_blocked_until = time.time() + _ABSENT_GRACE_S
        self._late_tail = None
        if last_error is None:
            last_error = self._api.last_error
        if (
            self.api_consecutive_failures >= self.api_failure_threshold
            and not self._api_notification_active
            and self.mqtt
        ):
            model = self.device_info.get("model", "S-series")
            # pragma: no mutate start
            reason = f" Last error: {last_error}." if last_error else ""
            # pragma: no mutate end
            msg = (
                f"The Nibe {model} REST API has not responded for "
                f"{self.api_consecutive_failures} consecutive polls "
                f"({self.api_consecutive_failures * self.bulk_interval}s). "
                f"Check that the controller is reachable at {self._api.base_url}.{reason} "
                f"For a detailed diagnostic, enable 'Debug mode' in the add-on "
                f"configuration and use the 'Test API Connection' button on the "
                f"Management device. "
                f"This notification will clear automatically when contact is restored."
            )
            self._notify(
                self.mqtt,
                title="Nibe Bridge: API Unreachable",
                message=msg,
                notification_id=_NOTIF_API_UNREACHABLE,
            )
            # Also publish a structured MQTT alert so automations and external
            # monitors can react without polling the HA notification bell.
            if self._pub:
                self._pub.publish_bridge_alert(
                    alert_type="api_unreachable",
                    severity="error",
                    message=msg,
                    context={
                        "consecutive_failures": self.api_consecutive_failures,
                        "failure_threshold": self.api_failure_threshold,
                        "api_url": self._api.base_url,
                        "last_error": last_error,
                    },
                )
            self._api_notification_active = True

    # ------------------------------------------------------------------ #
    # Dynamic point changes                                                #
    # ------------------------------------------------------------------ #

    def _publish_dynamic_changes(
        self,
        new_points: list[tuple[int, dict]],
        disappeared_points: set[int],
        controlling_point_id: int | None = None,
        controller_side: bool = False,
        controlling_value: int | None = None,
    ) -> None:
        """Add the points that appeared, record what the window's switch
        shows, remove the points that went away, and tell the card and the
        user.

        ``controlling_point_id`` and ``controller_side`` must come from the
        window the caller read (under ``_post_write_lock``) at the moment this
        scan cycle's post-write status was decided — not re-read from
        ``self._window`` here. So must ``controlling_value``, the value the
        window's switch was set to: appearing points are recorded under it
        only while the controller still reports it (recordable_value). The
        write executor doesn't block for the scan window, so a second write
        (to a different controlling switch) can overwrite that attribute
        while this call is still attributing the first write's detected
        changes; re-reading it here would risk crediting those changes to the
        wrong switch.
        """
        if not new_points and not disappeared_points:
            return

        # pragma: no mutate start
        log_discovery.info(
            "Publishing dynamic changes: +%d -%d", len(new_points), len(disappeared_points)
        )
        # pragma: no mutate end

        # 'triggered_by' is added below once added/removed are built.
        change_event: dict[str, object] = {
            "timestamp": time.time(),
            "iso_timestamp": _fmt_ts(),
            "added": [],
            "removed": [],
            "source": "firmware",
        }
        added: list[dict] = change_event["added"]  # type: ignore[assignment]
        removed: list[dict] = change_event["removed"]  # type: ignore[assignment]

        enabled_new_pids = self._add_appeared_points(new_points, added)
        self._record_window_outcome(enabled_new_pids, controlling_point_id, controlling_value)
        self._remove_disappeared_points(disappeared_points, removed)

        # enabled_new_pids (points that actually succeeded through
        # _enable_entity_locked), not the raw new_points list — matches
        # record_outcome's gating. If every point in new_points failed
        # to enable, nothing actually changed and these calls would just be
        # a wasted MQTT publish/persist cycle.
        if enabled_new_pids or disappeared_points:
            self.publish_enabled_state()

        if not added and not removed:
            return

        # Persist active_dynamic_points for appearance events.
        # Disappearances are already persisted per-point for crash safety.
        if enabled_new_pids:
            self._persist_active_dynamic()

        self._point_catalog_changed()

        trig = self._triggered_by(controlling_point_id, controller_side)
        change_event["triggered_by"] = trig
        self._update_changelog_history(change_event)
        self.mqtt.publish(BrowserTopic.DYNAMIC, json.dumps(change_event), retain=False)
        self._notify_dashboard_updated(added, removed, trig)

    def _add_appeared_points(
        self, new_points: list[tuple[int, dict]], added: list[dict]
    ) -> list[int]:
        """Index and enable each point that appeared as a dynamic point;
        returns the ones enabled, each also appended to ``added``."""
        enabled_new_pids: list[int] = []
        with self._em_lock, self._suppress_enabled_state():
            for point_id, point_data in new_points:
                # `or {}` covers an absent key and an explicit null alike.
                metadata = point_data.get("metadata") or {}
                title = clean_string(point_data.get("title", f"Point {point_id}"))
                description = clean_string(point_data.get("description", ""))
                processed = self._classify_and_index(point_id, metadata, title, description, True)
                entity_type = processed["entity_type"]
                self._pub.publish_point_metadata(processed)
                if not self._enable_entity_locked(point_id):
                    # Discovery publish failed — do not mark this point as
                    # active in our own bookkeeping when HA was never told
                    # about it. Leaving it out of active_dynamic_points also
                    # lets the reconciliation path retry activation on
                    # a future poll rather than presenting a permanently
                    # false "active" state.
                    continue
                self.active_dynamic_points.add(point_id)
                enabled_new_pids.append(point_id)

                added.append(
                    {
                        "id": point_id,
                        "title": title,
                        "type": entity_type,
                        "is_dynamic": True,
                    }
                )
                # pragma: no mutate start
                log_discovery.info(
                    "Dynamic entity appeared: %d - %s (%s)", point_id, title, entity_type
                )
                # pragma: no mutate end
        return enabled_new_pids

    def _record_window_outcome(
        self,
        enabled_new_pids: list[int],
        controlling: int | None,
        controlling_value: int | None,
    ) -> None:
        """Record the points that appeared as what the window's switch shows."""
        # Record all new points in dynamic_point_map via record_outcome
        # so the map is populated identically regardless of learning mode.
        # This ensures known_dynamic is correct for dashboard suppression
        # and injection — both in learning mode and normal operation.
        #
        # Uses enabled_new_pids (points that actually succeeded through
        # _enable_entity_locked in _add_appeared_points), not the raw
        # new_points list. A point whose discovery publish failed is
        # deliberately left out of active_dynamic_points so a later
        # reconciliation can retry it (see the comment at its `continue`) — but self.
        # published_configs is unconditionally set to the full current-poll
        # id set every cycle in _fetch_bulk_data, so a point that already
        # appeared once in bulk data can never re-enter new_points on a
        # later poll. If it were still recorded here as "known" despite the
        # failed enable, dynamic_point_map would believe it's an expected
        # dynamic point that HA was never actually told about, with no path
        # left to retry activation — the promised retry would silently
        # never happen.
        if not (controlling and enabled_new_pids):
            return
        current_raw = (self.bulk_data.get(controlling) or {}).get("raw_value")
        controlling_raw = recordable_value(controlling_value, current_raw)
        if controlling_raw is None:
            # pragma: no mutate start
            log_discovery.info(
                "Point(s) %s appeared in the scan window of point %d but aren't recorded "
                "as what it shows: it was set to %s and now reads %s, so they can't be "
                "told apart — learned on a later change",
                enabled_new_pids,
                controlling,
                "different values" if controlling_value is None else controlling_value,
                current_raw,
            )
            # pragma: no mutate end
            return
        new_pids = enabled_new_pids
        # dynamic_point_map._table is also mutated by a Force Poll or
        # Flush Dynamic Map on the management thread, and read/iterated by
        # populate_from_bulk — _em_lock serializes all of them.
        with self._em_lock:
            entry = self.dynamic_point_map.get(controlling)
            if entry is None:
                # First time this controlling point is seen — create the entry
                controlling_point = self.all_points_by_id.get(controlling, {})
                controlling_meta = controlling_point.get("metadata", {})
                mn = controlling_meta.get("minValue", 0)
                mx = controlling_meta.get("maxValue", 1)
                entry = DynamicPointEntry(
                    point_id=controlling,
                    title=controlling_point.get("display_title") or f"Point {controlling}",
                    entity_type=controlling_point.get("entity_type", "switch"),
                    unprocessed_values=set(range(int(mn), int(mx) + 1)),
                )
                self.dynamic_point_map._table[controlling] = entry
                # pragma: no mutate start
                log_discovery.debug(
                    "Created dynamic map entry for controlling point %d", controlling
                )
                # pragma: no mutate end
            # This controller's already-known dynamic points that are
            # still present (active and in this bulk response) belong to
            # this value too, even though they didn't newly appear: a
            # point shown under more than one of a select's values stays
            # put when switching between them. record_outcome replaces
            # the value's list, so recording only the new arrivals would
            # drop it — and at the next restart the startup reconcile
            # would then treat the still-live point as stale and remove
            # it. Empty whenever no such sharing exists.
            still_present = (
                entry.all_known_dynamic_points()
                & self.active_dynamic_points
                & self.bulk_data.keys()
            ) - set(new_pids)
            self.dynamic_point_map.record_outcome(
                controlling, controlling_raw, sorted(set(new_pids) | still_present)
            )
            self._persist_dynamic_map()
        # pragma: no mutate start
        log_discovery.debug(
            "Recorded %d dynamic point(s) under controlling point %d "
            "(value=%d) via post-write scan",
            len(new_pids),
            controlling,
            controlling_raw,
        )
        # pragma: no mutate end

    def _remove_disappeared_points(self, disappeared_points: set[int], removed: list[dict]) -> None:
        """Disable and deindex each dynamic point that went away, each
        appended to ``removed``."""
        with self._em_lock:
            for point_id in disappeared_points:
                entity = self.all_points_by_id.get(point_id)
                if entity:
                    # Disable before deindexing: _disable_entity_locked reads
                    # the point's metadata from all_points_by_id for its stats
                    # decrement, and got {} (a silent no-op) when it ran after
                    # _deindex_point, inflating the stats counts on every
                    # dynamic appear/disappear cycle.
                    if point_id in self.mqtt_enabled_points:
                        self._disable_entity_locked(point_id)
                    self._deindex_point(point_id)
                    self._pub.invalidate_config_hash(point_id)
                    self.mqtt.publish(
                        BrowserTopic.META_TEMPLATE.format(id=point_id), "", retain=True
                    )
                    self.active_dynamic_points.discard(point_id)
                    # An untitled point (the firmware sent no title) has a
                    # display_title of None.
                    removed.append(
                        {
                            "id": point_id,
                            "title": entity["display_title"] or f"Point {point_id}",
                            "type": entity["entity_type"],
                            "is_dynamic": True,
                        }
                    )
                    # pragma: no mutate start
                    log_discovery.info("Dynamic entity disappeared: %d", point_id)
                    # pragma: no mutate end
                    # Persist active set immediately after removal — write-ahead
                    # ordering so a crash after this line leaves correct state.
                    self._persist_active_dynamic()

    def _triggered_by(self, controlling: int | None, controller_side: bool) -> dict | None:
        """The change event's triggered_by: the window's switch, or None for
        startup / periodic-poll discoveries."""
        if not controlling:
            return None
        cp = self.all_points_by_id.get(controlling, {})
        ctrl_title = cp.get("display_title") or f"Point {controlling}"
        ctrl_value = self.bulk_data.get(controlling, {}).get("raw_value")
        return {
            "id": controlling,
            "title": ctrl_title,
            **(({"value": ctrl_value}) if ctrl_value is not None else {}),
            # Changed on the controller's own display, not written here.
            **({"changed_on": "controller"} if controller_side else {}),
        }

    def _notify_dashboard_updated(
        self, added: list[dict], removed: list[dict], trig: dict | None
    ) -> None:
        """Send a persistent HA notification so the user knows the dashboard
        was updated and needs a browser reload to show the changes."""
        try:
            ctrl_title = trig["title"] if trig else ""
            ctrl_menu = ""  # pragma: no mutate — None is equally falsy for the `if ctrl_menu:` check below when trig is falsy (never reassigned)
            if trig:
                menu_entry = self.point_to_menu_map.get(trig["id"])
                ctrl_menu = f"menu {menu_entry[0]} — {menu_entry[1]}" if menu_entry else ""
            # pragma: no mutate start
            log_discovery.debug(
                "Dynamic change notification: controlling=%s added=%d removed=%d",
                trig["id"] if trig else None,
                len(added),
                len(removed),
            )
            # pragma: no mutate end

            # Both sections share one HA persistent-notification ID —
            # notify_ha() creates-or-REPLACES by that ID, so sending two
            # separate notifications here (one for added, one for removed)
            # would silently drop whichever one is sent first whenever a
            # single poll cycle produces both (e.g. a mode-select point
            # swapping from one dynamic-point set to another). Build one
            # combined message instead. The ID is per triggering switch: two
            # switches changed shortly after each other each keep their own
            # notification, while one switch changed again replaces its own.
            if added or removed:
                ctrl_line = f"\nTriggered by: **{ctrl_title}**" if ctrl_title else ""
                menu_line = f" in {ctrl_menu}" if ctrl_menu else ""
                sections = []
                if added:
                    added_lines = "\n".join(f"- **{p['title']}** (point {p['id']})" for p in added)
                    sections.append(
                        f"{len(added)} new setting(s) are now available:\n\n{added_lines}"
                    )
                if removed:
                    removed_lines = "\n".join(
                        f"- **{p['title']}** (point {p['id']})" for p in removed
                    )
                    sections.append(
                        f"{len(removed)} setting(s) are no longer available:\n\n{removed_lines}"
                    )
                # The Nibe Menus dashboard only exists when mode == 'menus'
                # (see nibe_lovelace.py's provision_lovelace_ui docstring) —
                # referencing it in other modes would point the user at a
                # dashboard that was never created. The Nibe Bridge
                # dashboard (with the Entity Manager card) is provisioned
                # in every mode, so it's always a valid pointer instead.
                # File read (not the retained-topic version) matches the
                # existing precedent in restore_snapshot() — cheap, no MQTT
                # round-trip needed for a cosmetic wording choice.
                in_menus_mode = (
                    (self._applied_mode_known or self._read_applied_mode_from_file()) == "menus"
                )  # pragma: no mutate — mode-name comparison, no observable difference at the boundary

                if in_menus_mode:
                    title = "Nibe Menus — Dashboard updated"
                    message = (
                        f"The Nibe Menus dashboard was updated{menu_line}:"
                        f"{ctrl_line}\n\n"
                        + "\n\n".join(sections)
                        + "\n\n[Open Nibe Menus dashboard](/nibe-menus) and reload the page "
                        "to see the changes."
                    )
                else:
                    title = "Nibe Bridge — Available settings changed"
                    message = (
                        f"The set of available Nibe settings changed{menu_line}:"
                        f"{ctrl_line}\n\n"
                        + "\n\n".join(sections)
                        + "\n\n[Open the Nibe Bridge entity manager](/nibe-bridge) to see "
                        "the changes."
                    )
                self._notify(
                    self.mqtt,
                    title=title,
                    message=message,
                    notification_id=(
                        f"nibe_dashboard_updated_{trig['id']}" if trig else "nibe_dashboard_updated"
                    ),
                )
        except (ValueError, TypeError, AttributeError) as e:
            # pragma: no mutate start
            log_discovery.warning("Could not send dashboard update notification: %s", e)
            # pragma: no mutate end
        except Exception:
            # pragma: no mutate start
            log_discovery.exception("Unexpected error sending dashboard notification")
            # pragma: no mutate end

    # ------------------------------------------------------------------ #
    # Write commands                                                       #
    # ------------------------------------------------------------------ #

    def _parse_command_payload(
        self,
        payload: str,
        entity_info: dict,
        cmd_id: str,
    ) -> int | float | str | None:
        """Parse and validate a raw MQTT payload string for the given entity type.

        Returns the converted integer/float/string value to write to the API,
        or ``None`` if the payload is invalid and the command should be dropped.

        Separated from ``_handle_command`` so the conversion logic can be unit-
        tested independently of MQTT callback plumbing.
        """
        point_id = entity_info["point_id"]
        entity_type = entity_info["entity_type"]
        if entity_type == "button":
            return 1

        if entity_type in ("switch", "binary_sensor"):
            return 1 if payload in ("1", "ON", "on", "true", "True") else 0

        if entity_type == "time":
            # Convert HH:MM:SS (or HH:MM) from HA time entity to seconds.
            try:
                parts = payload.strip().split(":")
                h, m = int(parts[0]), int(parts[1])
                return h * 3600 + m * 60
            except (ValueError, IndexError):
                # pragma: no mutate start
                log_commands.warning(
                    "[%s] Invalid time value: '%s' — expected HH:MM:SS", cmd_id, payload
                )
                # pragma: no mutate end
                return None

        if entity_type == "select":
            mapping = get_value_mapping(point_id, entity_info["point_data"])
            if mapping:
                reverse_map = {v.strip(): k for k, v in mapping.items()}
                if payload in reverse_map:
                    return reverse_map[payload]
                # Translated select options (see _value_translations /
                # nibe_discovery_config.build_select_config) mean the
                # payload HA actually sends back is the translated label,
                # not the English one reverse_map above is keyed by — check
                # that too before falling through to the raw-int fallback.
                # Same {} default as everywhere else _value_translations is
                # read: an untranslated bridge (English, or no matching
                # translations file) makes this a no-op, reverse_map already
                # covered it above.
                translated_reverse_map = {
                    self._value_translations[v].strip(): k
                    for k, v in mapping.items()
                    if v in self._value_translations
                }
                if payload in translated_reverse_map:
                    return translated_reverse_map[payload]
                # Fallback: mapping labels can be built from the API's live
                # description text, which is language-dependent. If the
                # options published to HA were built under a different query
                # language than the mapping in hand now (e.g. a restart with
                # a changed `language` setting), the label round-trip above
                # fails even though the write itself is legitimate. Accept a
                # raw integer payload directly as long as it's one of the
                # mapping's own known keys, rather than silently dropping it.
                try:
                    raw_value = int(payload)
                except ValueError:
                    raw_value = None  # pragma: no mutate — mapping keys are always int (see parse_description_mapping/VALUE_MAPPINGS), so "" would never match either
                if raw_value is not None and raw_value in mapping:
                    return raw_value
                # pragma: no mutate start
                log_commands.warning("[%s] Invalid select option: '%s'", cmd_id, payload)
                # pragma: no mutate end
                return None
            try:
                return int(payload)
            except ValueError:
                # pragma: no mutate start
                log_commands.warning("[%s] Invalid numeric value: '%s'", cmd_id, payload)
                # pragma: no mutate end
                return None

        if entity_type == "number":
            divisor = entity_info["metadata"].get("divisor") or 1
            try:
                value = reverse_divisor(float(payload), divisor)
            except ValueError:
                # pragma: no mutate start
                log_commands.warning("[%s] Invalid number: '%s'", cmd_id, payload)
                # pragma: no mutate end
                return None
            if not entity_info.get("is_degenerate_range"):
                metadata = entity_info["metadata"]
                min_val = metadata.get("minValue")
                max_val = metadata.get("maxValue")
                if min_val is not None and value < min_val:
                    # pragma: no mutate start
                    log_commands.warning(
                        "[%s] Number out of range: %s < min %s for point %d",
                        cmd_id,
                        value,
                        min_val,
                        point_id,
                    )
                    # pragma: no mutate end
                    if point_id in self.last_states:
                        self.mqtt.publish(
                            entity_info["state_topic"],
                            self.last_states[point_id],
                            retain=True,
                        )
                    return None
                if max_val is not None and value > max_val:
                    # pragma: no mutate start
                    log_commands.warning(
                        "[%s] Number out of range: %s > max %s for point %d",
                        cmd_id,
                        value,
                        max_val,
                        point_id,
                    )
                    # pragma: no mutate end
                    if point_id in self.last_states:
                        self.mqtt.publish(
                            entity_info["state_topic"],
                            self.last_states[point_id],
                            retain=True,
                        )
                    return None
            return value

        if entity_type == "text":
            sanitised = "".join(c for c in payload if c.isprintable())
            if len(sanitised) > _TEXT_REGISTER_MAX_LEN:
                # pragma: no mutate start
                log_commands.warning(
                    "[%s] Text payload for point %d truncated from %d to %d chars",
                    cmd_id,
                    point_id,
                    len(sanitised),
                    _TEXT_REGISTER_MAX_LEN,
                )
                # pragma: no mutate end
                sanitised = sanitised[:_TEXT_REGISTER_MAX_LEN]
            if sanitised != payload:
                # pragma: no mutate start
                log_commands.debug(
                    "[%s] Text payload for point %d sanitised",
                    cmd_id,
                    point_id,
                )
                # pragma: no mutate end
            return sanitised

        return None  # unknown entity type — no write path

    def _opens_scan_window(self, point_id: int) -> bool:
        """Whether a write to this switch/select opens a scan window: all do
        except one learned as showing nothing (_handle_command_worker's case
        A1)."""
        entry = self.dynamic_point_map.get(point_id)
        return not (entry is not None and entry.is_fully_processed() and not entry.is_controlling)

    def _wait_for_learning_window(self, point_id: int, cmd_id: str) -> None:
        """Before a write that opens a scan window, wait until an open
        learning window has shown the effect of its own write — the point set
        changed (_release_learning_write) — or ended (_finish_learning_window).

        This write would take that window over, and the points still to come
        from the switch being learned would be linked to this one. Writes
        that open no window — numbers, buttons, times, a switch learned as
        showing nothing — don't wait, and neither does a write to the switch
        being learned itself: set to another value, its window turns
        ambiguous and records nothing (_open_post_write_scan), so holding
        the write back for up to 90 s would protect nothing. The wait ends
        early on shutdown (begin_shutdown sets the event too), and after a
        window plus a margin in case the poll loop stalls.
        """
        if self._learning_window_ended.is_set():
            return
        with self._post_write_lock:
            learning = self._window.point if self._window is not None else None
        if learning == point_id:
            return
        # pragma: no mutate start
        log_commands.info(
            "[%s] Write for point %d waits for the learning window of point %s "
            "(up to %ds) — it would otherwise take that window over",
            cmd_id,
            point_id,
            learning,
            self._post_write_duration,
        )
        # pragma: no mutate end
        self._learning_window_ended.wait(self._post_write_duration + _LEARNING_WAIT_MARGIN_S)

    def _release_learning_write(self) -> None:
        """Let the write worker take the next write once the point set has
        changed in a learning window — the firmware has shown the write's
        effect, and points that appeared are recorded by now. A user setting
        one of the points a switch just showed would otherwise wait out the
        rest of the 90 s window. The window and its learning target stay:
        _finish_learning_window still runs when it ends, unless a write to
        another switch takes the window over first — then the value stays to
        be learned on a later write (_open_post_write_scan)."""
        with self._post_write_lock:
            if self._window is not None and self._window.learning_value is not None:
                self._learning_window_ended.set()

    def _finish_learning_window(self, point_id: int, value: int, saw_points: bool) -> None:
        """Record what a write to a value not learned yet showed, now that its
        scan window has ended (update_all_states).

        Points that appeared during the window were recorded as they arrived
        (_publish_dynamic_changes). A window in which none appeared records
        "shows nothing" for the value — together with this switch's known
        points still present, which belong to every value showing them
        (record_outcome replaces the value's list). Nothing is recorded, and
        the value stays to be learned on a later write, when:

        - points did appear but weren't recorded under this value (e.g. their
          discovery publish failed, or the controller reports another value);
        - attribution is blocked (_attributable_poll): an API failure or a
          restarting controller in the window proves nothing about the value;
        - the bridge is shutting down, which cut the window short.

        The waiting write worker is released only afterwards, so the next
        write sees this outcome in the map.
        """
        try:
            self._record_learning_window(point_id, value, saw_points)
        finally:
            self._learning_window_ended.set()

    def _record_learning_window(self, point_id: int, value: int, saw_points: bool) -> None:
        """_finish_learning_window, without releasing the write worker."""
        if self._shutdown_event.is_set():
            return
        with self._em_lock:
            entry = self.dynamic_point_map.get(point_id)
            if entry is None:
                return
            outcome = learning_outcome(
                unprocessed=value in entry.unprocessed_values,
                saw_points=saw_points,
                blocked=time.time() < self._attribution_blocked_until,
            )
            if outcome == "learned":
                # pragma: no mutate start
                log_commands.info(
                    "Learning detection complete for point %d value=%d: %s",
                    point_id,
                    value,
                    entry.dynamic_points_by_value.get(value) or "no new point(s)",
                )
                # pragma: no mutate end
                return
            if outcome == "skip":
                # pragma: no mutate start
                log_commands.info(
                    "Learning window for point %d value=%d ended without a usable "
                    "outcome — the value will be learned on a later write",
                    point_id,
                    value,
                )
                # pragma: no mutate end
                return
            carried = sorted(
                entry.all_known_dynamic_points()
                & self.active_dynamic_points
                & self.bulk_data.keys()
            )
            self.dynamic_point_map.record_outcome(point_id, value, carried)
            self._persist_dynamic_map()
        # pragma: no mutate start
        log_commands.info(
            "Learning detection complete for point %d value=%d: no new point(s)%s",
            point_id,
            value,
            f", still showing {carried}" if carried else "",
        )
        # pragma: no mutate end

    def _handle_command(self, entity_info: dict, message: Any) -> None:
        """Decode the MQTT payload and dispatch a write to the worker thread.

        Payload parsing is delegated to ``_parse_command_payload`` so this method
        stays focused on MQTT decode, correlation ID generation, pending-write
        registration, and executor submission.

        Runs directly on paho's MQTT network thread (registered via
        ``message_callback_add``). paho is constructed with its default
        ``suppress_exceptions=False``, so an exception escaping this method
        propagates out of the network thread's read loop and permanently
        kills it — no more messages would ever be delivered. Everything past
        the payload decode is therefore wrapped in a catch-all, matching
        every sibling ``message_callback_add`` handler in this module.
        """
        try:
            payload = message.payload.decode("utf-8").strip()  # pragma: no mutate
        except UnicodeDecodeError:
            # pragma: no mutate start
            log_commands.warning("Malformed UTF-8 payload on topic %s — ignoring", message.topic)
            # pragma: no mutate end
            return

        try:
            point_id = entity_info["point_id"]
            cmd_id = uuid.uuid4().hex[:_CMD_ID_LENGTH]

            # pragma: no mutate start
            log_commands.info(
                "[%s] Command received for %s %d: '%s'",
                cmd_id,
                entity_info["entity_type"],
                point_id,
                payload,
            )
            # pragma: no mutate end

            value = self._parse_command_payload(payload, entity_info, cmd_id)  # pragma: no mutate
            if value is None:
                return

            entity_type = entity_info["entity_type"]
            # One read: the poll thread can remove the point at any moment.
            current = self.bulk_data.get(point_id)
            if entity_type != "button" and current is not None:
                # A "button" is a stateless action (e.g. a pulse/increment
                # command) — every press must reach the controller even if
                # the last-known value looks unchanged. Every other type is
                # a persisted setting: if the most recent poll already shows
                # this exact value, the write can only be a duplicate — a
                # retained/re-delivered MQTT command (a QoS>=1 broker is
                # explicitly allowed to deliver a message more than once),
                # or a "keep it set" automation that re-asserts a value on
                # every trigger regardless of current state. Skipping avoids
                # a needless PATCH to the physical controller.
                #
                # For a controlling switch/select, this also skips that
                # write's post-write dynamic-point scan window (see case A2
                # below) — acceptable, since nothing this write could have
                # changed if the value was already set; the regular poll
                # loop or a genuinely different write still catches dynamic
                # point changes from any other cause.
                current_value = (
                    current["string_value"] if entity_type == "text" else current.get("raw_value")
                )
                # bulk_data only reflects the last *confirmed* poll, which
                # can lag well behind an already-successful write still
                # awaiting confirmation (pending_writes isn't cleared until
                # _update_entity_state sees it — e.g. up to the 90s
                # post-write scan window above). Comparing a new write
                # against that stale value can mistake a genuine change —
                # e.g. turning a switch back off right after turning it on,
                # before the confirming poll lands — for a no-op duplicate
                # and silently drop it. While a write to this point is
                # still pending, that write's own value is the true
                # "current" one for this comparison, not bulk_data's.
                with self._pending_writes_lock:
                    pending_entry = self.pending_writes.get(point_id)
                if pending_entry is not None:
                    current_value = pending_entry["value"]
                if current_value == value:
                    # pragma: no mutate start
                    log_commands.info(
                        "[%s] Skipping write for point %d — value %s is already set",
                        cmd_id,
                        point_id,
                        value,
                    )
                    # pragma: no mutate end
                    return

            with self._pending_writes_lock:
                self.pending_writes[point_id] = {
                    "point_id": point_id,
                    "value": value,
                    "payload": payload,
                    "timestamp": time.time(),
                    "cmd_id": cmd_id,
                }
            self._publish_requested_state(entity_info, value, payload)

            self._submit_write(
                self._handle_command_worker,
                entity_info,
                value,
                payload,
                cmd_id,
                point_id=point_id,
                cmd_id_for_log=cmd_id,
            )
        except Exception:
            # pragma: no mutate start
            log_commands.exception(
                "Unhandled exception handling command on topic %s", message.topic
            )
            # pragma: no mutate end

    def _publish_requested_state(self, entity_info: dict, value: float | str, payload: str) -> None:
        """Show a write's value in HA as soon as its command arrives.

        HA's toggle (and slider, and drop-down) moves at once and flips back
        when the entity's state hasn't followed within two seconds; the
        controller takes about 1.5 s to accept a write, sometimes more, so
        publishing only after the write made a switch go on, off and on
        again. The pending write keeps a poll's stale value from replacing
        this one until the controller reports the new value. A write that
        fails puts the controller's own value back (_force_readback), and
        failing that, the next poll does, the pending write being gone.
        """
        state_topic = entity_info.get("state_topic")
        if not state_topic:
            return
        entity_type = entity_info["entity_type"]
        if entity_type == "switch":
            state_value = "1" if value else "0"
        elif entity_type == "time":
            secs = int(value) % 86400
            state_value = f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}:00"
        elif entity_type in ("select", "number"):
            state_value = payload
        else:
            state_value = str(value)
        self.mqtt.publish(state_topic, state_value, retain=True)
        with self._em_lock:
            self.last_states[entity_info["point_id"]] = state_value
            self._state_sent.add(entity_info["point_id"])

    def _submit_write(
        self,
        fn: Callable,
        *args: Any,
        point_id: int,
        cmd_id_for_log: str,
    ) -> None:
        """Submit a write-handler call to ``_write_executor`` with logging.

        A bare ``self._write_executor.submit(fn, *args)`` silently swallows
        any exception ``fn`` raises — nothing awaits the returned Future or
        checks ``.result()``, so a bug in the handler fails invisibly with
        no log line and the point's ``pending_writes`` entry is never
        explicitly cleared. Wrapping it here ensures every write failure is
        at least logged, mirroring ``ManagementCommandHandler._submit``, and
        releases that entry: a write that died before it started would
        otherwise never age out, and the entity would stop updating.

        ``_write_executor`` has a single worker, and a write to a switch or
        select waits there for up to ``_post_write_duration`` seconds while
        another value is being learned (so that one value is learned at a
        time — see _wait_for_learning_window) — so a write submitted behind
        it won't even start until it finishes. If several switches are flipped in quick succession, each
        one landing in this situation stacks the wait additively (e.g. three
        such writes can take up to 3x that duration before the last one is
        even attempted), which is not obvious from either write's own log
        lines alone. Check here, before submitting, and say so if so.

        Depth is counted directly rather than inferred from whether a
        learning detection is currently running: that state is only
        established once the *previous* write's PATCH has already returned,
        so several switches flipped together (an HA scene, say) all submit
        inside that gap and would every one of them look unqueued. It also
        deliberately reports how many writes are ahead rather than an
        estimated wait in seconds — how long each of those takes depends on
        whether it opens a detection window at all, which isn't known until
        it runs, so any total here would be a guess stated as a fact.
        """
        with self._write_queue_lock:
            ahead = self._write_queue_depth
            self._write_queue_depth += 1
        if ahead > 0:
            # pragma: no mutate start
            log_commands.info(
                "[%s] Write for point %d queued behind %d unfinished write(s) — writes run "
                "one at a time, and a switch or drop-down can hold the queue for up to %ds "
                "while another is being learned",
                cmd_id_for_log,
                point_id,
                ahead,
                self._post_write_duration,
            )
            # pragma: no mutate end

        def _wrapped() -> None:
            try:
                fn(*args)
            except Exception:
                with self._pending_writes_lock:
                    self.pending_writes.pop(point_id, None)
                # pragma: no mutate start
                log_commands.exception("Unhandled exception in write command handler")
                # pragma: no mutate end
            finally:
                with self._write_queue_lock:
                    self._write_queue_depth -= 1

        try:
            self._write_executor.submit(_wrapped)
        except RuntimeError:
            # submit() raises once the executor has been shut down, so
            # _wrapped never runs and never decrements. Undo the increment
            # here or the count stays permanently high and every later write
            # reports a queue that isn't there.
            with self._write_queue_lock:
                self._write_queue_depth -= 1
            raise

    def begin_shutdown(self) -> None:
        """Signal in-flight write work to stop waiting and wind up.

        Call before draining ``_write_executor``: a write waiting for its
        learning window otherwise holds the single write worker for the rest
        of that 90s window, which is longer than the whole shutdown budget, so the
        drain cannot complete and the executors that share that budget never
        get a chance to drain at all.

        Also signals the API client, whose retry can hold a worker for
        roughly as long again — see ``NibeApiClient.begin_shutdown``.

        Idempotent, and safe to call even if no write is in flight.
        """
        self._shutdown_event.set()
        self._learning_window_ended.set()
        self._api.begin_shutdown()

    def _open_post_write_scan(
        self, point_id: int, learning_value: int | None = None, value: int | None = None
    ) -> None:
        """Activate the post-write scan window for a write to ``point_id``.

        ``value``: the value written (defaults to ``learning_value``). A
        second write to the same switch with a different value while its
        window is open makes the window ambiguous (value None): points the
        first value shows can still arrive — the REST API's point list lags
        its values by up to a minute — and would be recorded under the
        second, so nothing in that window is recorded, and a value being
        learned stays to be learned later.

        ``learning_value``: the value written, when it isn't learned yet —
        the poll loop then records its outcome when the window ends
        (_finish_learning_window), and the next write that opens a window
        waits for that (_wait_for_learning_window).
        A window opened for another point drops a learning target still set
        (the worker gave up waiting on a stalled poll loop): what appears now
        can't be told apart, so that value is learned on a later write.

        The window is replaced whole under ``_post_write_lock``, so a reader
        never sees one write's point with another's timing.
        """
        if value is None:
            value = learning_value
        with self._post_write_lock:
            previous = self._window
            saw_points = False
            if previous is not None and previous.point == point_id and previous.value != value:
                # Ambiguous: two values of its own switch.
                value = learning_value = None
                if previous.learning_value is not None:
                    self._learning_window_ended.set()
            elif learning_value is not None:
                self._learning_window_ended.clear()
            elif (
                previous is not None
                and previous.learning_value is not None
                and previous.point == point_id
            ):
                learning_value, saw_points = previous.learning_value, previous.saw_points
            elif previous is not None and previous.learning_value is not None:
                self._learning_window_ended.set()
            self._window = ScanWindow(
                point=point_id,
                until=time.time() + self._post_write_duration,
                learning_value=learning_value,
                saw_points=saw_points,
                value=value,
            )

    def _handle_command_worker(
        self,
        entity_info: dict,
        value: float | str,
        payload: str,
        cmd_id: str = "",
    ) -> None:
        """Execute a write and handle the outcome.

        For writable switches and selects, post-write behaviour is determined
        by what the DynamicPointMap knows about this point.  See inline
        comments for the four cases (A1, A2, A3a, A3b/B).

        ``cmd_id`` is a short correlation token generated in ``_handle_command``
        so all log lines for a single write can be tied together even when
        interleaved with poll-loop output.
        """
        point_id = entity_info["point_id"]
        entity_type = entity_info["entity_type"]
        prefix = f"[{cmd_id}] " if cmd_id else ""  # pragma: no mutate

        log_commands.info("%sWriting %s to point %d", prefix, value, point_id)  # pragma: no mutate

        if entity_type in ("switch", "select") and self._opens_scan_window(point_id):
            self._wait_for_learning_window(point_id, cmd_id)

        # Mark the moment this write actually left the queue and began
        # executing. _update_entity_state's staleness check ages from here,
        # not from when the command arrived — see _STALE_WRITE_AGE_S.
        with self._pending_writes_lock:
            pending_entry = self.pending_writes.get(point_id)
            if pending_entry is not None:
                pending_entry["started_at"] = time.time()

        self._write_total += 1
        success = self._api.write_point(point_id, value, entity_info)

        if success:
            self._write_success += 1
            # pragma: no mutate start
            log_commands.info("%sWrite successful for point %d", prefix, point_id)
            # pragma: no mutate end
            if self._write_notification_active and self.mqtt:
                self._dismiss(self.mqtt, _NOTIF_WRITE_ERROR)
                self._write_notification_active = False
                if self._pub:
                    self._pub.publish_bridge_alert(
                        alert_type="write_restored",
                        severity="info",
                        message=f"Write to point {point_id} succeeded — previous error cleared.",
                        context={"point_id": point_id, "cmd_id": cmd_id},
                    )

            # ── Post-write dynamic point handling ─────────────────────────────
            # Only switches and selects can be controlling points.
            # Two cases per the simplified design:
            #
            # Case A1 — fully processed, non-controlling:
            #   No scan window needed. Done.
            #
            # Case A2 — fully processed, controlling:
            #   Activate post-write scan window (90s, 5s poll).
            #   Bulk fetch will activate/deactivate known dynamic points.
            #
            # Case A3 / B — unprocessed value or not in map:
            #   Activate post-write scan window.
            #   Learning is always active: if the value is unprocessed, a
            #   detection window runs and the outcome is recorded in the map.
            if entity_type in ("switch", "select"):
                # _parse_command_payload gives a switch or select an int.
                int_value = int(value)
                entry = self.dynamic_point_map.get(point_id)

                if entry is not None and entry.is_fully_processed() and not entry.is_controlling:
                    # Case A1: non-controlling — no scan needed. Points it
                    # does show after all (learned from a window they came
                    # too late for) are still linked when they arrive.
                    # pragma: no mutate start
                    log_commands.debug(
                        "%sPoint %d is non-controlling — no dynamic scan needed",
                        prefix,
                        point_id,
                    )
                    # pragma: no mutate end
                    self._start_late_tail(point_id, int_value, False, after=_POST_WRITE_SCAN_S)

                elif (
                    entry is not None
                    and entry.is_fully_processed()
                    and entry.is_controlling
                    and not entry.firmware_removed
                ):
                    # Case A2: fully-processed controlling — open the post-write
                    # scan window. The bulk fetch will detect dynamic point
                    # appearances/disappearances within the 90s window.
                    # (Fast-path single-point probing was removed: hardware testing
                    # on the S2125 showed the firmware takes >12.5s to activate
                    # a dynamic point after a REST write, so all probes missed and
                    # the post-write scan caught it anyway.)
                    # pragma: no mutate start
                    log_commands.debug(
                        "%sPoint %d is fully-processed controlling — opening scan window "
                        "(up to %ds to detect new points)",
                        prefix,
                        point_id,
                        self._post_write_duration,
                    )
                    # pragma: no mutate end
                    self._open_post_write_scan(point_id, value=int_value)

                else:
                    # Case A3 / B: unprocessed value or not in map.
                    # DynamicPointMap learning is always active — outcomes are
                    # always recorded so the map self-populates without manual
                    # intervention. The detection window runs and any dynamic
                    # changes observed are attributed to this write.
                    learning_value = (
                        int_value
                        if entry is not None and int_value in entry.unprocessed_values
                        else None
                    )
                    self._open_post_write_scan(point_id, learning_value, value=int_value)
                    # pragma: no mutate start
                    if learning_value is not None:
                        log_commands.info(
                            "%sPoint %d value=%d is unprocessed — starting detection window "
                            "(learning always active, up to %ds to detect new points)",
                            prefix,
                            point_id,
                            learning_value,
                            self._post_write_duration,
                        )
                    else:
                        # Kept at INFO (not debug) so a first-time-seen point's
                        # scan window is visible at the default log level,
                        # with its expected duration, rather than only its
                        # "window ended" line — otherwise a user watching the
                        # log at default verbosity sees a point appear with
                        # no indication of how long detection might take.
                        log_commands.info(
                            "%sPost-write scan activated for point %d (%s) — "
                            "up to %ds to detect new points",
                            prefix,
                            point_id,
                            "unprocessed" if entry is not None else "not in map",
                            self._post_write_duration,
                        )
                    # pragma: no mutate end

        else:
            self._write_failed += 1
            self._last_write_error = f"point {point_id} value '{payload}' at {_fmt_ts()}"
            log_commands.error("%sWrite failed for point %d", prefix, point_id)  # pragma: no mutate
            # Pop pending_writes on failure so the point isn't blocked
            # from normal state updates by a stale pending entry.
            with self._pending_writes_lock:
                self.pending_writes.pop(point_id, None)
            # The title is in the indexed point, not in entity_info itself —
            # read from there, the notification always said "point N (point N)".
            point_title = (
                entity_info.get("point_data", {}).get("display_title") or f"point {point_id}"
            )
            if self.mqtt and not self._write_notification_active:
                msg = (
                    f"Could not write value '{payload}' to {point_title} "
                    f"(point {point_id}). The controller rejected or did not respond. "
                    f"The entity has been reverted to its last known value. "
                    f"This notification will clear automatically on the next "
                    f"successful write."
                )
                self._notify(
                    self.mqtt,
                    title="Nibe Bridge: Write Failed",
                    message=msg,
                    notification_id=_NOTIF_WRITE_ERROR,
                )
                if self._pub:
                    self._pub.publish_bridge_alert(
                        alert_type="write_failed",
                        severity="error",
                        message=msg,
                        context={
                            "point_id": point_id,
                            "point_title": point_title,
                            "value": payload,
                            "cmd_id": cmd_id,
                            "write_failed_total": self._write_failed,
                        },
                    )
                self._write_notification_active = True
            self._force_readback(entity_info)

    def _force_readback(self, entity_info: dict) -> None:
        """Fetch the live value for a single point and republish to HA.

        Called after a write failure so the HA entity snaps back to the actual
        controller state rather than displaying the rejected optimistic value.

        The single-point endpoint returns the data block under the JSON key
        "value" (not "datavalue" — that is the value of the inner type field).
        Confirmed against real SMO S40 firmware responses.
        """
        point_id = entity_info["point_id"]
        response = self._api.fetch_point(point_id)
        if not response:
            # pragma: no mutate start
            log_commands.debug(
                "Force readback for point %d skipped — point absent "
                "(dynamic point inactive or network error)",
                point_id,
            )
            # pragma: no mutate end
            return
        dv = response.get("value", {})  # pragma: no mutate
        if not isinstance(dv, dict) or not dv.get("isOk"):
            # pragma: no mutate start
            log_commands.warning("Force readback for point %d: value not OK", point_id)
            # pragma: no mutate end
            return
        # Fresh firmware metadata, so it gets the same corrections as at
        # discovery and on every poll (apply_metadata_overrides).
        self._process_and_publish_state(
            entity_info,
            dv.get("integerValue", 0),
            dv.get("stringValue", ""),
            apply_metadata_overrides(point_id, response.get("metadata") or {}),
            force=True,
        )

    # ------------------------------------------------------------------ #
    # MQTT reconnect helpers                                               #
    # ------------------------------------------------------------------ #

    def register_mgmt_subscription(self, topic: str, handler: Callable, qos: int = 1) -> None:
        """Record a management topic subscription so it survives broker restarts.

        Called by create_management_handlers() once for each management topic
        (aid/smart mode, alarm reset, force poll, enable/disable entity,
        changelog-read).  The (topic, handler, qos) triple is stored so
        resubscribe_all() can replay it after a Mosquitto restart clears the
        broker-side subscriptions.

        Parameters
        ----------
        topic   : MQTT topic string, e.g. "homeassistant/switch/nibe_aid_mode/set"
        handler : paho message callback ``(client, userdata, message) -> None``
        qos     : QoS level (default 1)
        """
        with self._mgmt_subscriptions_lock:
            self._mgmt_subscriptions.append((topic, handler, qos))

    def resubscribe_all(self) -> None:
        """Re-register ALL subscriptions after an MQTT broker reconnect.

        Paho's clean-session reconnect clears every broker-side subscription.
        This method restores two classes of subscription:

        1. Per-entity command topics (switch/number/select set topics)
           — iterate active_entities_by_id.
        2. Management topics (aid/smart mode, alarm reset, force poll,
           enable/disable entity, changelog-read button)
           — replay _mgmt_subscriptions registered by create_management_handlers().

        Without replaying group 2 the frontend card buttons and HA management
        entities go silent after any Mosquitto restart.

        The sent-state record is cleared on reconnect so that all entity states
        are republished immediately rather than waiting for a value change —
        HA loses all retained state when the broker restarts.

        That record and last_bulk_fetch are reset under _em_lock — this runs
        on paho's own MQTT network thread (via on_connect), concurrently with
        the poll loop thread reading/writing them in update_all_states().

        _mgmt_subscriptions is snapshotted under _mgmt_subscriptions_lock
        before iterating — register_mgmt_subscription() can still be appending
        to it from the main thread (during create_management_handlers(), which
        runs after the MQTT loop is already started) while this method runs
        concurrently on the MQTT thread.
        """
        with self._em_lock:
            self.last_bulk_fetch = 0
            # The broker may have lost its retained messages: the poll this
            # forces resends every state, availability and cleared state
            # included.
            self._state_sent.clear()
            self._availability_sent.clear()
            self._state_cleared.clear()
        entity_count = 0
        with self._active_entities_lock:
            snapshot = list(self.active_entities_by_id.values())
        for entity_info in snapshot:
            topic = entity_info.get("command_topic")
            if not topic:
                continue

            def _make_handler(ei: dict) -> Callable:
                def handler(_client: Any, _userdata: Any, message: Any) -> None:
                    self._handle_command(ei, message)

                return handler

            self.mqtt.subscribe(topic, qos=1)
            self.mqtt.message_callback_add(topic, _make_handler(entity_info))
            entity_count += 1

        mgmt_count = 0
        with self._mgmt_subscriptions_lock:
            mgmt_subscriptions_snapshot = list(self._mgmt_subscriptions)
        for topic, handler, qos in mgmt_subscriptions_snapshot:
            self.mqtt.subscribe(topic, qos=qos)
            self.mqtt.message_callback_add(topic, handler)
            mgmt_count += 1

        # Re-subscribe to internal changelog topics (set up in _setup_history_loading).
        # These are retained topics — re-subscribing after a broker restart causes
        # the broker to re-deliver the retained payload, which refreshes the in-memory
        # change_history and the unread badge state.
        self.mqtt.subscribe(BrowserTopic.CHANGELOG_HISTORY)
        self.mqtt.message_callback_add(BrowserTopic.CHANGELOG_HISTORY, self._on_history_message)
        self.mqtt.subscribe(BrowserTopic.CHANGELOG_UNREAD)
        self.mqtt.message_callback_add(BrowserTopic.CHANGELOG_UNREAD, self._on_unread_message)

        # Re-subscribe to the dynamic_point_map and active_dynamic_points retained
        # topics so state is refreshed from the broker after a reconnect.
        self.mqtt.subscribe(BrowserTopic.DYNAMIC_MAP)
        self.mqtt.message_callback_add(BrowserTopic.DYNAMIC_MAP, self._on_dynamic_map_message)
        self.mqtt.subscribe(BrowserTopic.ACTIVE_DYNAMIC)
        self.mqtt.message_callback_add(BrowserTopic.ACTIVE_DYNAMIC, self._on_active_dynamic_message)

        # pragma: no mutate start
        log_mqtt.info(
            "Reconnect: re-subscribed to %d entity command topic(s), "
            "%d management topic(s), 2 changelog topic(s), and dynamic map",
            entity_count,
            mgmt_count,
        )
        # pragma: no mutate end

    def republish_retained_state(self) -> None:
        """Republish everything the bridge keeps as retained MQTT state.

        Retained messages survive a broker restart only if the broker persists
        them — Mosquitto doesn't by default, and even with persistence it saves
        only periodically, so a crash loses recent changes. The bridge treats
        retained discovery configs as the record of which entities exist, and
        reconnecting used to restore subscriptions, availability and states
        only. After a broker lost its retained messages: the card was empty,
        every entity vanished at Home Assistant's next restart, and the
        bridge's own next restart found no configs, took it for a fresh
        install and re-applied the mode, dropping manual additions.

        Called on every reconnect and when Home Assistant announces itself
        online (its MQTT birth message). Idempotent when the broker kept
        everything — the same payloads again. Resends the removals of dynamic
        points without an entity first (_resend_dynamic_removals).
        """
        if not self.initial_discovery_complete or not self._pub:
            return
        # Never once shutdown has begun: with remove_frontend, shutdown clears
        # every retained topic the bridge owns, and a reconnect landing in
        # that window (this runs straight from on_connect) would put them all
        # back, undoing an intentional uninstall cleanup.
        if self._shutdown_event.is_set():
            return
        # Before the configs below: a point that appears in between is
        # republished by them.
        self._resend_dynamic_removals()
        with self._active_entities_lock:
            snapshot = list(self.active_entities_by_id.values())
        for entity_info in snapshot:
            point_id = entity_info["point_id"]
            point = self.all_points_by_id.get(point_id) or entity_info.get("point_data")
            if point is None:
                continue
            self._pub.forget_published_hashes(point_id)
            self._pub.publish_entity_discovery(point, self.bulk_data)
        if self._mgmt_discovery_args is not None:
            self._pub.publish_management_discovery(*self._mgmt_discovery_args)
        self._pub.publish_all_metadata(self.all_points)
        self._pub.publish_point_list(self.all_points_by_id)
        # Straight to the publisher: publish_enabled_state() would treat an
        # unchanged set as nothing to do and skip the dashboard callback
        # anyway, but this is a republish, not a change.
        self._pub.publish_enabled_state(self.mqtt_enabled_points)
        self._persist_wanted_points()
        self._persist_reclassified_points()
        if self._applied_mode_known:
            self.mqtt.publish(BrowserTopic.APPLIED_MODE, self._applied_mode_known, retain=True)
        self._persist_dynamic_map()
        self._persist_active_dynamic()
        self.publish_snapshots()
        self._publish_changelog_history()
        # pragma: no mutate start
        log_mqtt.info(
            "Republished retained state: %d entity configs, catalog, enabled set, "
            "dynamic map, snapshots and changelog",
            len(snapshot),
        )
        # pragma: no mutate end

    def _publish_changelog_history(self) -> None:
        """Publish the changelog as it stands and its unread count, continuing
        its _seq so a card that already saw the old one accepts it."""
        with self._em_lock:
            unread_count = sum(1 for e in self.change_history if e["unread"])
            self._history_seq += 1
            history_payload = {
                "history": list(self.change_history),
                "total_entries": len(self.change_history),
                "unread_count": unread_count,
                "last_updated": time.time(),
                "_seq": self._history_seq,
            }
        self.mqtt.publish(
            BrowserTopic.CHANGELOG_HISTORY, _compress_payload(history_payload), retain=True
        )
        self.mqtt.publish(
            BrowserTopic.CHANGELOG_UNREAD,
            json.dumps({"unread_count": unread_count, "last_change": time.time()}),
            retain=True,
        )
        # Only after the publishes. A crash in between then leaves
        # _last_published_seq behind _history_seq, so the retained copy read
        # back on restart isn't filtered by the seq guard. And a reconnect
        # racing the publish (resubscribe_all replays these callbacks) can't
        # redeliver the old retained message with a _seq the guard already
        # counts as published and let it overwrite the newer state.
        self._last_published_seq = self._history_seq

    def republish_availability(self) -> None:
        """Republish 'online' for all active entities after a broker restart.

        The management/bridge availability topic is republished
        unconditionally on _mgmt_avail_topic being set, independent of
        whether there happen to be any active entities — an early return
        here when active_entities_by_id is empty (e.g. zero entities
        enabled, a legitimate state) would otherwise leave the bridge's own
        top-level "available" sensor stuck on the LWT's "offline" forever
        after a reconnect, with nothing left to bring it back online.
        """
        with self._active_entities_lock:
            snapshot = list(self.active_entities_by_id.values())
        # After a reconnect the broker may have lost everything: forget what
        # was sent, so the next poll sends each entity's real availability.
        self._availability_sent.clear()
        self._state_cleared.clear()
        for entity_info in snapshot:
            self._publish_availability(entity_info, "online")
        if self._mgmt_avail_topic:
            self.mqtt.publish(self._mgmt_avail_topic, "online", retain=True)
        # pragma: no mutate start
        if snapshot or self._mgmt_avail_topic:
            log_mqtt.info("Reconnect: republished availability for %d entities", len(snapshot))
        # pragma: no mutate end

    # ------------------------------------------------------------------ #
    # Enabled-state publish                                                #
    # ------------------------------------------------------------------ #

    def publish_enabled_state(self) -> None:
        """Publish the current enabled-point list to MQTT for the frontend card."""
        self._pub.publish_enabled_state(self.mqtt_enabled_points)
        # Only fire the callback when the enabled set has actually changed.
        # publish_enabled_state() is called in many places; firing on every
        # call caused the menu dashboard regeneration to trigger hundreds of
        # times, filling the HA system log with "URL already in use" errors.
        current = frozenset(self.mqtt_enabled_points)
        if self._on_enabled_state_change is not None and current != self._last_published_enabled:
            self._last_published_enabled = current
            try:
                self._on_enabled_state_change()
            except Exception as e:  # noqa: BLE001 — best-effort; logged and degrades gracefully
                # pragma: no mutate start
                log_entities.warning("on_enabled_state_change callback error: %s", e)
                # pragma: no mutate end
        else:
            self._last_published_enabled = current

    def set_on_enabled_state_change(self, callback: Callable) -> None:
        """Register a callback invoked whenever the enabled entity set changes."""
        self._on_enabled_state_change = callback

    # ------------------------------------------------------------------ #
    # Entity-ID resolution (used by HAEntityRegistryWatcher)              #
    # ------------------------------------------------------------------ #

    def resolve_point_from_entity_id(
        self,
        ha_entity_id: str,
        unique_id_map: dict | None = None,
    ) -> int | None:
        """Resolve a Nibe point_id from a Home Assistant entity_id string.

        Three-pass lookup:
          Pass 1 — slug prefix O(1).
          Pass 2 — active entity config topic scan O(n).
          Pass 3 — unique_id registry map O(1) (when provided by the watcher).
        """
        if "." not in ha_entity_id:
            return None
        domain, slug = ha_entity_id.split(".", 1)

        if slug.startswith("nibe_"):
            try:
                return int(slug[5:])
            except ValueError:
                pass

        candidate = f"homeassistant/{domain}/{slug}/config"
        with self._active_entities_lock:
            for pid, ei in self.active_entities_by_id.items():
                known = f"homeassistant/{ei['entity_type']}/{ei['entity_id']}/config"
                if known == candidate:
                    return pid

        if unique_id_map:
            for uid, eid in unique_id_map.items():
                if eid == ha_entity_id and uid.startswith("nibe_"):
                    try:
                        return int(uid[5:])
                    except ValueError:
                        pass

        return None

    def ha_disable_notif_id(self, ha_entity_id: str) -> str:
        """Return the notification_id used for HA-side disable/re-enable
        notifications for a given entity.

        Shared by build_disable_notification() (called when the entity is
        disabled, to create the notification) and
        HAEntityRegistryWatcher._on_entity_enabled() in nibe_ha_integration.py
        (called when the entity is later re-enabled, to dismiss it) — these
        two call sites MUST compute the exact same string for the same
        ha_entity_id, or a disable notification created by one path can
        never be dismissed by the other, leaving a stale notification in
        HA forever after the user re-enables the entity. This used to be
        duplicated inline in both places (byte-identical, but with nothing
        enforcing that agreement); extracted here so there is only one
        place this logic can drift.
        """
        safe_id = ha_entity_id.replace(".", "_").replace("-", "_")[:60]
        return f"nibe_ha_disable_{safe_id}"

    def build_disable_notification(
        self,
        point_id: int | None,
        ha_entity_id: str,
        action: str,
    ) -> tuple[str, str, str]:
        """Build (title, message, notification_id) for a HA-side entity disable/enable event."""
        point = self.all_points_by_id.get(point_id) if point_id else None
        # pragma: no mutate start
        is_dynamic = point.get("is_dynamic", False) if point else False
        # pragma: no mutate end

        if point:
            display = f"#{point_id} ({point.get('display_title') or f'Point {point_id}'})"
        elif point_id:
            display = f"#{point_id}"
        else:
            display = ha_entity_id

        notif_id = self.ha_disable_notif_id(ha_entity_id)

        if action == "re-enabled":
            return (
                "Nibe Bridge: Entity re-enabled in HA",
                (
                    f"Data point {display} was re-enabled via the HA entity settings. "
                    f"The bridge will resume publishing its state on the next poll cycle."
                ),
                notif_id,
            )

        if is_dynamic:
            # Look up the controlling switch/select so the notification can
            # name it rather than describing the mechanism generically.
            controller = (
                self.dynamic_point_map.controlling_entry_for_dynamic(point_id) if point_id else None
            )
            if controller:
                context = (
                    f"This entity appeared because of a change to "
                    f'"{controller.title}" (#{controller.point_id}). '
                    "It will disappear automatically when that switch/select "
                    "is no longer in the state that activates it."
                )
            else:
                context = (
                    "This entity appeared during a firmware-controlled state change. "
                    "It will disappear automatically when the operating mode that "
                    "activates it is no longer active."
                )
            return (
                "Nibe Bridge: Dynamic entity disabled in HA",
                (
                    f"Dynamic data point {display} was disabled via the HA entity settings. "
                    f"The bridge has kept the entity enabled — it is still being polled.\n\n"
                    f"Please go to Settings > Entities and re-enable it.\n\n{context}"
                ),
                notif_id,
            )

        return (
            "Nibe Bridge: Entity disabled in HA",
            (
                f"Data point {display} was disabled via the HA entity settings. "
                f"The bridge has kept the entity enabled — this point is still being polled.\n\n"
                f"Please go to Settings > Entities and re-enable it.\n\n"
                f"To remove a data point from HA use the Nibe Entity Manager card — "
                f"not the HA entity settings."
            ),
            notif_id,
        )

    # ------------------------------------------------------------------ #
    # Changelog                                                            #
    # ------------------------------------------------------------------ #

    def mark_changelog_read(self) -> None:
        """Mark all changelog entries as read."""
        log_history.info("Marking all changelog entries as read")  # pragma: no mutate
        # change_history is also mutated from the poll thread
        # (_update_changelog_history) and the MQTT callback thread
        # (_setup_history_loading's handlers) — _em_lock (an RLock,
        # so safe to nest with callers that already hold it) serializes
        # all of them against each other.
        with self._em_lock:
            for entry in self.change_history:
                entry["unread"] = False
        self._publish_changelog_history()

    def _prune_changelog_if_due(self) -> bool:
        """Prune changelog entries older than changelog_retention_days.

        Runs at most once per hour — the deque's maxlen already prevents
        unbounded growth, so pruning on every insert is unnecessary.
        Returns True if pruning ran (useful for tests).

        Keeps at least 50 entries regardless of age so the changelog is
        never wiped entirely on a long-running installation, and every
        firmware update entry (source "firmware_change").
        """
        now = time.time()
        if now - self._last_prune_time < _CHANGELOG_PRUNE_S:
            return False
        self._last_prune_time = now

        retention_days = self.changelog_retention_days
        cutoff_ts = now - retention_days * 86400

        # _em_lock is an RLock — safe to nest under _update_changelog_history,
        # which already holds it when calling this.
        with self._em_lock:
            valid = [
                e
                for e in self.change_history
                if (
                    isinstance(e, dict)
                    and "added" in e
                    and "removed" in e
                    and "timestamp" in e
                    and "iso_timestamp" in e
                )
            ]
            recent: list[dict] = []
            old: list[dict] = []
            for e in valid:
                (recent if e["timestamp"] >= cutoff_ts else old).append(e)

            needed = max(0, _CHANGELOG_MIN_ENTRIES - len(recent))
            # Firmware updates are kept whatever their age: a few a year, and
            # they say when points appeared or changed — worth more than the
            # ordinary appearances that would push them out. The deque's
            # _CHANGELOG_MAX_ENTRIES cap still applies.
            # (valid is newest first, so filtering keeps that order.)
            keep_ids = {id(e) for e in recent + old[:needed]}
            kept = [e for e in valid if id(e) in keep_ids or e.get("source") == "firmware_change"]

            pruned = len(self.change_history) - len(kept)
            if pruned > 0:
                self.change_history = deque(kept, maxlen=self.change_history.maxlen)
                # pragma: no mutate start
                log_history.debug("Changelog pruned: removed %d expired entries", pruned)
                # pragma: no mutate end
        return True

    def _update_changelog_history(self, change_event: dict) -> None:
        """Append a change event to the persistent changelog and republish to MQTT."""
        history_entry = {
            "timestamp": change_event.get("timestamp", time.time()),
            "iso_timestamp": change_event.get("iso_timestamp", _fmt_ts()),
            "added": change_event.get("added", []),
            "removed": change_event.get("removed", []),
            "id": f"change_{int(time.time() * 1000)}",
            "unread": True,
            "source": change_event.get("source", "firmware"),
            "triggered_by": change_event.get("triggered_by"),
        }
        if change_event.get("note"):
            history_entry["note"] = change_event["note"]
        if change_event.get("changed"):
            history_entry["changed"] = change_event["changed"]
        # change_history is also mutated from mark_changelog_read (MQTT
        # command-handler thread) and _setup_history_loading's callbacks
        # (MQTT client thread) — _em_lock serializes all of them.
        with self._em_lock:
            self.change_history.appendleft(history_entry)
            # Time-based pruning runs at most hourly — the deque hard cap
            # already prevents unbounded growth between prune cycles.
            self._prune_changelog_if_due()
        self._publish_changelog_history()

    def _setup_history_loading(self) -> None:
        """Subscribe to retained changelog topics to reload history on startup."""

        def on_history_message(_client: Any, _userdata: Any, message: Any) -> None:
            try:
                if not message.payload:
                    return
                raw = _decompress_payload(message.payload)
                data = json.loads(raw.decode("utf-8"))  # pragma: no mutate
                incoming_seq = data.get("_seq", -1)
                if incoming_seq != -1 and incoming_seq == self._last_published_seq:
                    return
                # Once this process has published, its in-memory history is
                # the newest there is, and anything below its last _seq is
                # its own earlier publish echoing back late — which used to
                # reload the older history over the newer one, losing the
                # latest entry for good with the next publish.
                # A payload without _seq (an older bridge's) always loads.
                if (
                    incoming_seq != -1
                    and isinstance(incoming_seq, int)
                    and self._last_published_seq
                    and incoming_seq < self._last_published_seq
                ):
                    return
                # change_history is also mutated from the poll thread and
                # mark_changelog_read — _em_lock (RLock) serializes them
                # against this MQTT-client-thread callback.
                with self._em_lock:
                    # Continue the sequence rather than restart it: the card
                    # discards a history whose _seq isn't above the last one
                    # it saw, so restarting at 1 on every bridge restart left
                    # an open card ignoring changelog updates until reloaded.
                    if isinstance(incoming_seq, int) and incoming_seq > self._history_seq:
                        self._history_seq = incoming_seq
                    if "history" in data and isinstance(data["history"], list):
                        clean_history: deque = deque(maxlen=self.change_history.maxlen)
                        for entry in data["history"]:
                            if isinstance(entry, dict):
                                cleaned = {
                                    "timestamp": entry.get("timestamp", time.time()),
                                    "iso_timestamp": entry.get("iso_timestamp", _fmt_ts()),
                                    "added": entry.get("added", []),
                                    "removed": entry.get("removed", []),
                                    "id": entry.get("id", f"change_{int(time.time() * 1000)}"),
                                    "unread": entry.get("unread", False),
                                    "source": entry.get("source", "firmware"),
                                    "triggered_by": entry.get("triggered_by"),
                                }
                                # A firmware update's versions and changed
                                # points: optional, as _update_changelog_history
                                # writes them. Left out here, a restart wiped
                                # them from the changelog for good.
                                if isinstance(entry.get("note"), str) and entry["note"]:
                                    cleaned["note"] = entry["note"]
                                if isinstance(entry.get("changed"), list) and entry["changed"]:
                                    cleaned["changed"] = entry["changed"]
                                if isinstance(cleaned["added"], list) and isinstance(
                                    cleaned["removed"], list
                                ):
                                    clean_history.append(cleaned)
                        self.change_history = clean_history
                        # Force prune on load regardless of last-prune timestamp so
                        # stale entries don't accumulate across restarts with no changes.
                        self._last_prune_time = 0.0  # pragma: no mutate
                        self._prune_changelog_if_due()
                        # pragma: no mutate start
                        log_history.info(
                            "Loaded %d historical changes from MQTT", len(self.change_history)
                        )
                        unread = sum(1 for e in self.change_history if e["unread"])
                        if unread > 0:
                            log_history.info("%d unread changes", unread)
                        # pragma: no mutate end
            except Exception as e:  # noqa: BLE001 — best-effort; logged and degrades gracefully
                # pragma: no mutate start
                log_history.warning(
                    "Could not load changelog history from MQTT retained message "
                    "(the message may be from an older bridge version): %s",
                    e,
                )
                # pragma: no mutate end
                with self._em_lock:
                    self.change_history = deque(maxlen=self.change_history.maxlen)

        def on_unread_message(_client: Any, _userdata: Any, message: Any) -> None:
            try:
                if not message.payload:
                    return
                data = json.loads(message.payload.decode("utf-8"))  # pragma: no mutate
                unread_count = data.get("unread_count", 0)
                with self._em_lock:
                    for entry in self.change_history:
                        entry["unread"] = False
                    if unread_count > 0 and self.change_history:
                        # deque doesn't support slice notation — convert to list
                        # for the head operation.  The list is temporary and small.
                        # Cap to history length: after a prune, unread_count from the
                        # broker may exceed the number of surviving entries; without
                        # the cap list[:n] returns all entries, over-marking them.
                        # Entries are appendleft()-ed everywhere (_update_changelog_
                        # history), so index 0 is the NEWEST entry — take the head
                        # ([:n]), not the tail ([-n:]), which would mark the oldest
                        # entries unread instead of the actually-recent ones.
                        safe_count = min(unread_count, len(self.change_history))
                        for entry in list(self.change_history)[:safe_count]:
                            entry["unread"] = True
            except Exception as e:  # noqa: BLE001 — best-effort; logged and degrades gracefully
                # pragma: no mutate start
                log_history.warning(
                    "Could not restore changelog unread state from MQTT: %s",
                    e,
                )
                # pragma: no mutate end

        # Store callbacks so resubscribe_all() can replay them after a broker restart.
        self._on_history_message = on_history_message
        self._on_unread_message = on_unread_message

        self.mqtt.subscribe(BrowserTopic.CHANGELOG_HISTORY)
        self.mqtt.message_callback_add(BrowserTopic.CHANGELOG_HISTORY, on_history_message)
        self.mqtt.subscribe(BrowserTopic.CHANGELOG_UNREAD)
        self.mqtt.message_callback_add(BrowserTopic.CHANGELOG_UNREAD, on_unread_message)

    def _persist_dynamic_map(self) -> None:
        """Persist the DynamicPointMap to MQTT (primary) and file (fallback).

        Called whenever the map changes — after record_outcome, after
        populate_from_bulk, and after flush.  Write-through: both stores
        are updated on every call.
        """
        payload = _compress_payload(json.loads(self.dynamic_point_map.serialise()))
        self.mqtt.publish(BrowserTopic.DYNAMIC_MAP, payload, retain=True)
        self.dynamic_point_map.to_file()
        # pragma: no mutate start
        log_discovery.debug("Persisted DynamicPointMap (%d entries)", len(self.dynamic_point_map))
        # pragma: no mutate end

    def _persist_active_dynamic(self) -> None:
        """Persist the active_dynamic_points set to MQTT.

        Called whenever active_dynamic_points changes — on dynamic point
        activation, reconciliation, and disappearance.  The retained message drives
        startup reconciliation: on restart the bridge re-enables exactly the
        points that were active when it shut down.
        """
        payload = json.dumps(sorted(self.active_dynamic_points))
        self.mqtt.publish(BrowserTopic.ACTIVE_DYNAMIC, payload, retain=True)
        # pragma: no mutate start
        log_discovery.debug("Persisted %d active dynamic point(s)", len(self.active_dynamic_points))
        # pragma: no mutate end

    def _persist_wanted_points(self, path: str | None = None) -> None:
        """Persist the _wanted_points set. Write-ahead: file first, then the
        retained MQTT topic — same ordering rationale as _persist_applied_mode.

        path defaults to None (resolved to _WANTED_POINTS_FILE at call time)
        so patching the module constant in tests actually takes effect.
        """
        self._persist_point_ids(
            self._wanted_points,
            path or _WANTED_POINTS_FILE,
            BrowserTopic.WANTED_POINTS,
            "wanted-points",
        )
        # pragma: no mutate start
        log_discovery.debug("Persisted %d wanted point(s)", len(self._wanted_points))
        # pragma: no mutate end

    def _persist_reclassified_points(self, path: str | None = None) -> None:
        """Persist _binary_sensor_reclassified. Write-ahead: file first, then
        the retained MQTT topic — same ordering as _persist_wanted_points."""
        self._persist_point_ids(
            self._binary_sensor_reclassified,
            path or _RECLASSIFIED_POINTS_FILE,
            BrowserTopic.RECLASSIFIED_POINTS,
            "reclassified-points",
        )

    def _persist_point_ids(self, point_ids: set[int], path: str, topic: str, what: str) -> None:
        """A point-id set to its fallback file, then its retained topic."""
        payload = json.dumps(sorted(point_ids))
        try:
            atomic_write_text(path, payload)
        except OSError as e:
            # pragma: no mutate start
            log_discovery.warning("Could not write %s fallback file: %s", what, e)
            # pragma: no mutate end
        self.mqtt.publish(topic, payload, retain=True)

    def _mark_wanted(self, point_id: int) -> None:
        """Record point_id as user-wanted and persist, if not already recorded."""
        if point_id not in self._wanted_points:
            self._wanted_points.add(point_id)
            self._persist_wanted_points()

    def _excluded_wanted_points(self, keep: set[int], protected: set[int]) -> set[int]:
        """Wanted points a replacing selection (a replace-mode switch, a flush
        restore) excludes but its disable loop can't reach, because they
        aren't enabled: points disabled after _ABSENT_GRACE_S of absence stay
        wanted so that their return re-enables them — which would undo the
        new selection. Known dynamic points are excluded; the dynamic path
        owns them."""
        return (
            (self._wanted_points - self.mqtt_enabled_points)
            - keep
            - protected
            - self.dynamic_point_map.all_known_dynamic_point_ids()
        )

    def _unmark_wanted(self, point_id: int) -> None:
        """Remove point_id from the wanted set and persist, if it was present."""
        if point_id in self._wanted_points:
            self._wanted_points.discard(point_id)
            self._persist_wanted_points()

    def _reconcile_wanted_points(self, current_point_ids: set[int]) -> None:
        """Re-enable any wanted point that is present in the current bulk
        fetch but not currently enabled. Called after every bulk fetch.

        Deliberately does not re-mark points as wanted (they already are) and
        does not touch points that are wanted but still absent — those are
        left for a later fetch to pick up.
        """
        with self._em_lock:
            for point_id in self._wanted_points & current_point_ids:
                if point_id not in self.mqtt_enabled_points:
                    # pragma: no mutate start
                    log_restore.info("Wanted point %d reappeared — re-enabling", point_id)
                    # pragma: no mutate end
                    self._enable_entity_locked(point_id)

    def _expire_absent_at_startup(self, current_point_ids: set[int]) -> None:
        """Resolve points that were absent at startup (see _absent_at_startup)
        after a successful bulk fetch: one that has come back stops being
        tracked (_reconcile_wanted_points re-enables it, since it is wanted);
        one still absent after _ABSENT_GRACE_S gets its retained discovery
        config cleared, so Home Assistant removes the entity. It stays
        wanted, same as the runtime absence path, in case it ever returns."""
        now = time.time()
        with self._em_lock:
            for point_id, since in list(self._absent_at_startup.items()):
                if point_id in current_point_ids:
                    del self._absent_at_startup[point_id]
                elif now - since >= _ABSENT_GRACE_S:
                    # pragma: no mutate start
                    log_entities.info(
                        "Point %d still absent %ds after startup — removing its entity",
                        point_id,
                        int(now - since),
                    )
                    # pragma: no mutate end
                    self._clear_retained_discovery(point_id)
                    self._pub.invalidate_config_hash(point_id)
                    del self._absent_at_startup[point_id]

    def _report_entity_id_changes(self) -> None:
        """Tell the user which entity ids a type change replaced.

        A point whose entity type changes (an update reclassifying it, or a
        binary sensor reclassified at runtime) is a new entity in Home
        Assistant, under a new entity id: automations, scripts and
        dashboards using the old one stop working. The publisher records
        each change; once none has followed for _ID_CHANGE_QUIET_S and the
        registry knows every new id (or _ID_CHANGE_MAX_WAIT_S has passed),
        one notification lists them, old → new.

        The old id is the registry's, when the watcher already knew it at
        the time of the change; at startup — where an update's changes
        happen — it doesn't run yet, so the old id is derived from the new
        one: HA generates both from the device and entity name, so they
        differ only in the domain unless the user renamed the entity. Each
        line names the entity and point too, so a renamed one is still
        recognisable.
        """
        now = time.time()
        changes = self._pub.type_changes
        for _ in range(len(changes)):
            point_id, old_domain, new_domain, title = changes.popleft()
            known = self.entity_id_resolver(point_id)
            entry = self._id_changes.setdefault(
                point_id,
                {
                    "old_domain": old_domain,
                    "old_id": known if known and known.startswith(f"{old_domain}.") else None,
                    "since": now,
                },
            )
            entry["new_domain"] = new_domain
            entry["title"] = title
            self._id_changes_last = now
        if not self._id_changes or now - self._id_changes_last < _ID_CHANGE_QUIET_S:
            return

        new_ids = {}
        for point_id, entry in self._id_changes.items():
            new_id = self.entity_id_resolver(point_id)
            if new_id and new_id.startswith(f"{entry['new_domain']}."):
                new_ids[point_id] = new_id
        oldest = min(entry["since"] for entry in self._id_changes.values())
        if len(new_ids) < len(self._id_changes) and now - oldest < _ID_CHANGE_MAX_WAIT_S:
            return

        lines = []
        for point_id, entry in sorted(self._id_changes.items()):
            new_id = new_ids.get(point_id)
            old_id = entry["old_id"] or (
                f"{entry['old_domain']}.{new_id.split('.', 1)[1]}" if new_id else None
            )
            old_text = f"`{old_id}`" if old_id else f"a `{entry['old_domain']}` entity"
            new_text = f"`{new_id}`" if new_id else f"a new `{entry['new_domain']}` entity"
            lines.append(f"- {entry['title']} (point {point_id}): {old_text} → {new_text}")
        message = (
            "These entities changed type, so Home Assistant gave them a new entity id. "
            "Update automations, scripts and dashboards that use the old one; the history "
            "continues under the new id from now on. If you had renamed one of these "
            "entities, its old id was the one you chose.\n\n" + "\n".join(lines)
        )
        # pragma: no mutate start
        log_entities.info("Entity ids changed by a type change: %d entities", len(lines))
        # pragma: no mutate end
        self._notify(
            self.mqtt,
            title="Nibe Bridge: entity ids changed",
            message=message,
            notification_id=_NOTIF_ENTITY_IDS_CHANGED,
        )
        self._id_changes.clear()

    def _check_firmware_update(self, current_point_ids: set[int]) -> None:
        """Record a firmware update in the changelog.

        An update is recognised by a changed version register
        (_FIRMWARE_VERSION_POINTS). That isn't watertight: 2509 holds the
        SMO S40's major.minor only, so a release that only raises the patch
        level goes unnoticed. In NIBE's changelog those are 14 of 69
        releases, and the ones since the local REST API exists (4.7.6, 4.8.7,
        4.12.8) changed no registers. Points appearing or changing without a
        version change can't be told apart from a setting or connected
        hardware, so they only renew the record.

        The record holds every static point's firmware_fingerprint (its
        register as the firmware describes it: type, size, range, divisor,
        unit, with any override's value put back) and a checksum over them;
        titles and the bridge's own entity types are kept for the entry's
        text but are not part of the checksum. Steady polls only compare the
        point set and the identity of each point's metadata object
        (_fetch_bulk_data replaces it only when its content changed). After a
        change, and once per process start, the full comparison runs once the
        point set has been stable for _ABSENT_GRACE_S — a restarting
        controller serves a partial list for about a minute, the same across
        several polls. With a version change it writes one changelog entry
        (source "firmware_change"): the versions, and the static points
        added, removed and changed. A language change renews the record
        silently, since units follow the language.

        Dynamic points are compared one by one, the moment each appears,
        with its own recorded fingerprint (kept while it is hidden): a
        setting can show or hide one but not change its description, so a
        difference is the firmware's, version change or not. Their changes go
        into the next update entry, or, once the point set is stable, into an
        entry of their own. Which dynamic points are shown is recorded too: an
        update keeps the controller's settings, so at a version change the
        ones shown before but not after count as removed by it, and the ones
        newly shown as added. Between updates, showing or hiding one is a
        setting and reported as nothing.
        """
        now = time.time()
        if current_point_ids != self._point_set_last:
            self._point_set_last = set(current_point_ids)
            self._point_set_stable_since = now
        with self._em_lock:
            # bulk_data mirrors the point set, so a changed set changes this
            # identity too: the check below covers both.
            # A copy, as a Force Poll's fetch can change the set during this loop.
            identity = hash(tuple(id(d.get("metadata")) for d in list(self.bulk_data.values())))
            if identity != self._metadata_identity:
                self._metadata_identity = identity
                self._firmware_check_due = True
                self._dynamic_checked.clear()
            dynamic_ids = self.dynamic_point_map.all_known_dynamic_point_ids() | set(
                self.active_dynamic_points
            )
            to_check = (current_point_ids & dynamic_ids) - self._dynamic_checked
        if to_check:
            self._check_dynamic_points(to_check)
            self._dynamic_checked |= to_check

        if now - self._point_set_stable_since < _ABSENT_GRACE_S:
            return
        if not self._firmware_check_due:
            if self._dynamic_changes:
                self._write_firmware_entry([], [], [], {}, {})
            return
        self._firmware_check_due = False

        record = self._load_firmware_record()
        language = self._api.language if isinstance(self._api.language, str) else ""
        with self._em_lock:
            versions = {
                str(pid): data.get("raw_value")
                for pid in _FIRMWARE_VERSION_POINTS
                if (data := self.bulk_data.get(pid)) and data.get("is_ok")
            }
            points = {
                str(pid): self._describe_point(pid)
                for pid in sorted(current_point_ids - dynamic_ids)
            }
        checksum = _fingerprint_checksum(points)
        present = sorted(str(pid) for pid in current_point_ids & dynamic_ids)
        if (
            record is None
            or record.get("language", language) != language
            or "checksum" not in record
        ):
            # First run, a language change, or a record from before the
            # checksum: take the current state as the reference. Dynamic
            # points already present get recorded on the next poll.
            self._save_firmware_record(versions, points, checksum, language, present)
            self._dynamic_checked.clear()
            return
        old_versions = record.get("versions") or {}
        # A version register not read ok this poll keeps its recorded version:
        # dropped from the record, the update that follows couldn't be seen.
        versions = {**old_versions, **versions}
        if not any(
            pid in old_versions and old_versions[pid] != raw for pid, raw in versions.items()
        ):
            # No version change: whatever changed is a setting, connected
            # hardware, or a patch-only release the version register can't
            # show — it only renews the record.
            if (
                checksum != record["checksum"]
                or versions != old_versions
                or present != record.get("dynamic_present")
            ):
                self._save_firmware_record(versions, points, checksum, language, present)
            if self._dynamic_changes:
                self._write_firmware_entry([], [], [], {}, {})
            return

        # A version changed: compare everything. The update keeps the
        # controller's settings, so the dynamic points shown before it should
        # be shown again — the ones that aren't, or are new, are the update's.
        old_points = record.get("points") or {}
        added = [
            {"id": int(key), "title": entry[0], "type": entry[1]}
            for key, entry in points.items()
            if key not in old_points
        ]
        removed = [
            {"id": int(key), "title": entry[0], "type": entry[1]}
            for key, entry in old_points.items()
            if key not in points
        ]
        old_present = record.get("dynamic_present")
        if old_present is not None:
            known = record.get("dynamic") or {}
            for key in present:
                if key not in old_present:
                    entry = self._describe_point(int(key))
                    added.append({"id": int(key), "title": entry[0], "type": entry[1]})
            for key in old_present:
                if key not in present:
                    entry = known.get(key) or [f"Point {key}", "unknown"]
                    removed.append({"id": int(key), "title": entry[0], "type": entry[1]})
        added.sort(key=lambda e: e["id"])
        removed.sort(key=lambda e: e["id"])
        changed = []
        for key, entry in points.items():
            old_entry = old_points.get(key)
            if old_entry and len(old_entry) >= 3:
                changes = _describe_fingerprint_change(old_entry[2], entry[2])
                if changes:
                    changed.append(
                        {"id": int(key), "title": entry[0], "type": entry[1], "changes": changes}
                    )
        self._write_firmware_entry(added, removed, changed, old_versions, versions, points)
        self._save_firmware_record(versions, points, checksum, language, present)

    def _describe_point(self, pid: int) -> list:
        """[title, entity type, firmware_fingerprint] for the firmware record."""
        point = self.all_points_by_id.get(pid, {})
        return [
            point.get("display_title") or f"Point {pid}",
            point.get("entity_type", "unknown"),
            firmware_fingerprint(self.bulk_data[pid].get("metadata") or {}),
        ]

    def _check_dynamic_points(self, point_ids: set[int]) -> None:
        """Compare dynamic points that just appeared with their own record
        (_check_firmware_update); record ones not seen before."""
        record = self._load_firmware_record()
        if record is None:
            return  # nothing recorded yet: the first full comparison sets it up
        recorded = record.setdefault("dynamic", {})
        with self._em_lock:
            seen = {str(pid): self._describe_point(pid) for pid in sorted(point_ids)}
        written = False
        for key, entry in seen.items():
            old_entry = recorded.get(key)
            if old_entry and len(old_entry) >= 3 and old_entry[2] != entry[2]:
                self._dynamic_changes.append(
                    {
                        "id": int(key),
                        "title": entry[0],
                        "type": entry[1],
                        "changes": _describe_fingerprint_change(old_entry[2], entry[2]),
                    }
                )
            if old_entry != entry:
                recorded[key] = entry
                written = True
        if written:
            self._write_firmware_record_file()

    def _write_firmware_entry(
        self,
        added: list,
        removed: list,
        changed: list,
        old_versions: dict,
        versions: dict,
        points: dict | None = None,
    ) -> None:
        """One firmware_change changelog entry, including the dynamic point
        changes collected so far."""
        changed = sorted(changed + self._dynamic_changes, key=lambda e: e["id"])
        self._dynamic_changes = []
        titles = points or {}
        version_changes = [
            pid for pid in versions if pid in old_versions and old_versions[pid] != versions[pid]
        ]
        if version_changes:
            note = "; ".join(
                f"{titles.get(pid, [f'Point {pid}'])[0]}: "
                f"{_format_version(int(pid), old_versions[pid])} → "
                f"{_format_version(int(pid), versions[pid])}"
                for pid in version_changes
            )
        else:
            note = "Found when these dynamic points were shown again."
        # pragma: no mutate start
        log_discovery.info(
            "Firmware change detected (%s): %d point(s) added, %d removed, %d changed",
            note,
            len(added),
            len(removed),
            len(changed),
        )
        # pragma: no mutate end
        self._update_changelog_history(
            {
                "added": added,
                "removed": removed,
                "changed": changed,
                "source": "firmware_change",
                "note": note,
            }
        )

    def _load_firmware_record(self) -> dict | None:
        """The firmware record, read from _FIRMWARE_RECORD_FILE once."""
        if not self._firmware_record_loaded:
            self._firmware_record_loaded = True
            try:
                with open(_FIRMWARE_RECORD_FILE, encoding="utf-8") as f:
                    loaded = json.loads(f.read())
                if isinstance(loaded, dict) and isinstance(loaded.get("versions"), dict):
                    self._firmware_record = loaded
            except (OSError, ValueError):
                pass
        return self._firmware_record

    def _save_firmware_record(
        self, versions: dict, points: dict, checksum: str, language: str, present: list
    ) -> None:
        """Replace the firmware record (keeping the per-point dynamic
        fingerprints) and persist it; present is the dynamic points shown."""
        dynamic = (self._firmware_record or {}).get("dynamic") or {}
        self._firmware_record = {
            "versions": versions,
            "language": language,
            "checksum": checksum,
            "points": points,
            "dynamic": dynamic,
            "dynamic_present": present,
        }
        self._write_firmware_record_file()

    def _write_firmware_record_file(self) -> None:
        try:
            atomic_write_text(_FIRMWARE_RECORD_FILE, json.dumps(self._firmware_record))
            log_discovery.debug("Firmware record written")  # pragma: no mutate
        except OSError as e:
            log_discovery.warning("Could not write the firmware record: %s", e)  # pragma: no mutate

    def _persist_applied_mode(self, mode_name: str, path: str | None = None) -> None:
        """Persist the last-applied entity mode. Called at the end of apply_mode().

        Write-ahead: the file fallback is written first, then the retained
        MQTT topic — so a crash between the two never loses the record (the
        file is always at least as current as the broker).

        path defaults to None (resolved to the module-level
        _APPLIED_MODE_FILE at call time, not bound at function-definition
        time) so that patching the module constant — e.g. in tests —
        actually takes effect; a plain default argument would freeze the
        original value forever.
        """
        if path is None:
            path = _APPLIED_MODE_FILE
        try:
            atomic_write_text(path, mode_name)
        except OSError as e:
            # pragma: no mutate start
            log_restore.warning("Could not write applied-mode fallback file: %s", e)
            # pragma: no mutate end
        self.mqtt.publish(BrowserTopic.APPLIED_MODE, mode_name, retain=True)
        self._applied_mode_known = mode_name
        log_restore.debug("Persisted applied mode: %s", mode_name)  # pragma: no mutate

    def _read_applied_mode_from_file(self, path: str | None = None) -> str | None:
        """Read the last-applied mode from the file fallback. None if absent,
        unreadable, or not a mode this build recognises.

        See _persist_applied_mode for why path resolves dynamically
        instead of using a plain default argument.
        """
        if path is None:
            path = _APPLIED_MODE_FILE
        try:
            with open(path, encoding="utf-8") as f:
                mode = f.read().strip()
            return _known_mode_or_none(mode or None)
        except OSError:
            return None

    def read_applied_mode(self) -> str | None:
        """Read the last-applied entity mode: retained MQTT topic first, file fallback.

        Returns None when neither store has a record — this is the migration
        boundary (first startup after deploying the mode system, or a broker
        that has been wiped) and callers should treat it as "unknown; do not
        assume a mode change," per decide_startup_action().

        Uses the same synchronous subscribe-and-wait pattern as
        scan_mqtt_discovery(), sized for a single retained topic rather than
        a full discovery-config scan.
        """
        received: threading.Event = threading.Event()
        result: list[str | None] = [None]

        def on_message(_client: Any, _userdata: Any, message: Any) -> None:
            if message.payload:
                try:
                    result[0] = message.payload.decode("utf-8").strip() or None  # pragma: no mutate
                except UnicodeDecodeError:
                    result[0] = None
            received.set()

        self.mqtt.subscribe(BrowserTopic.APPLIED_MODE)
        self.mqtt.message_callback_add(BrowserTopic.APPLIED_MODE, on_message)
        try:
            received.wait(timeout=_APPLIED_MODE_TIMEOUT_S)
        finally:
            self.mqtt.message_callback_remove(BrowserTopic.APPLIED_MODE)
            self.mqtt.unsubscribe(BrowserTopic.APPLIED_MODE)

        # Validate the retained copy too, not just the file fallback — a
        # record this build doesn't recognise is equally uninformative
        # whichever store it came from. Falling through to the file when the
        # broker's copy is unusable is deliberate: it may well hold a valid
        # record. See _known_mode_or_none.
        broker_mode = _known_mode_or_none(result[0])
        mode = broker_mode if broker_mode is not None else self._read_applied_mode_from_file()
        if mode is not None:
            self._applied_mode_known = mode
        return mode

    def _reconcile_dynamic_points(self) -> None:
        """Reconcile dynamic point state after startup bulk fetch.

        Compares three sets:
        - expected_active  : derived from dynamic_point_map + current bulk values
        - persisted_active : loaded from ACTIVE_DYNAMIC retained topic
        - bulk_present     : actually in the current bulk fetch

        Three outcomes per point:

        1. In expected AND in bulk fetch
           → ensure enabled + discovery published + in active set.
           Normal resume after restart.

        2. In expected but NOT in bulk fetch
           → remove from active set, clear discovery, remove from persistence.
           Firmware update removed the point or it disappeared while bridge was down.

        3. In persisted_active but NOT in expected
           → stale entry (controlling switch changed while bridge was offline).
           Remove from active set, clear discovery, remove from persistence.
        """
        if not self.initial_discovery_complete:
            return

        # A copy, as a Force Poll's fetch can change the set during this method.
        bulk_snapshot = dict(self.bulk_data)
        current_raw_values = {pid: bd["raw_value"] for pid, bd in bulk_snapshot.items()}
        bulk_present = set(bulk_snapshot)
        expected_active = self.dynamic_point_map.expected_active_dynamic_points(current_raw_values)
        persisted_active = set(self.active_dynamic_points)  # snapshot

        activated = 0
        removed = 0

        with self._em_lock, self._suppress_enabled_state():
            # Suppress publish_enabled_state() for the duration of the
            # enable/disable loops below — same reasoning as apply_mode's
            # bulk enable/disable loop: without it, each individual
            # _enable_entity_locked/_disable_entity_locked call fires its
            # own publish_enabled_state() (since _is_suppressed() would
            # otherwise be False the whole time), flooding MQTT with N
            # intermediate publishes instead of the single one at the end
            # of this method.
            # Case 1 + 2: process expected active set
            for point_id in expected_active:
                if point_id in bulk_present:
                    # Point is expected and present — ensure it is active
                    if point_id not in self.mqtt_enabled_points:
                        # In bulk_present: a bulk entry, which always carries
                        # metadata, title and description (_update_bulk_entry).
                        point_data = bulk_snapshot[point_id]
                        self._classify_and_index(
                            point_id,
                            point_data["metadata"],
                            point_data["title"],
                            point_data["description"],
                            True,
                        )
                        if not self._enable_entity_locked(point_id):
                            # Discovery publish failed — leave this point out
                            # of active_dynamic_points so it isn't falsely
                            # marked active when HA was never told about it;
                            # this same reconcile pass will retry it next time.
                            continue
                        self.active_dynamic_points.add(point_id)
                        # activated's exact count only affects the final log
                        # line and `if activated or removed:`'s truthiness
                        # (any non-zero value, however produced, is equally
                        # truthy) — never a returned or otherwise-read value.
                        # pragma: no mutate start
                        activated += 1
                        log_discovery.debug(
                            "Reconcile: activated dynamic point %d '%s'",
                            point_id,
                            point_data["title"],
                        )
                        # pragma: no mutate end
                    else:
                        # Already enabled — still need to publish online and
                        # current state so HA doesn't show the entity as unavailable.
                        with self._active_entities_lock:
                            entity_info = self.active_entities_by_id.get(point_id)
                        if entity_info:
                            self.mqtt.publish(
                                entity_info["availability_topic"], "online", retain=True
                            )
                            self._update_entity_state(entity_info)
                        self.active_dynamic_points.add(point_id)
                else:
                    # Point is expected but absent from bulk fetch
                    if point_id in self.active_dynamic_points:
                        self._clear_retained_discovery(point_id)
                        self._deindex_point(point_id)
                        self._pub.invalidate_config_hash(point_id)
                        self.mqtt.publish(
                            BrowserTopic.META_TEMPLATE.format(id=point_id), "", retain=True
                        )
                        self.active_dynamic_points.discard(point_id)
                        # removed's exact count only affects the final log
                        # line and `if activated or removed:`'s truthiness —
                        # see the matching note at `activated += 1` above.
                        # pragma: no mutate start
                        removed += 1
                        log_discovery.info(
                            "Reconcile: removed stale dynamic point %d "
                            "(expected but absent from bulk — firmware update?)",
                            point_id,
                        )
                        # pragma: no mutate end

            # Case 3: stale persisted entries not in expected set
            stale = persisted_active - expected_active
            for point_id in stale:
                self._clear_retained_discovery(point_id)
                self._deindex_point(point_id)
                self._pub.invalidate_config_hash(point_id)
                self.mqtt.publish(BrowserTopic.META_TEMPLATE.format(id=point_id), "", retain=True)
                self.active_dynamic_points.discard(point_id)
                # pragma: no mutate start
                removed += 1
                log_discovery.info(
                    "Reconcile: removed stale dynamic point %d "
                    "(in persisted active but not in expected — controlling switch changed?)",
                    point_id,
                )
                # pragma: no mutate end

        if activated or removed:
            self._persist_active_dynamic()
            self._persist_dynamic_map()
            self.publish_enabled_state()

        # pragma: no mutate start
        log_discovery.info(
            "Startup reconciliation: %d dynamic point(s) activated, %d removed",
            activated,
            removed,
        )
        # pragma: no mutate end

    def _clear_retained_discovery(self, point_id: int) -> None:
        """Remove a point's HA entity once the bridge has decided the point is
        gone — a dynamic point the startup reconciliation found stale, or a
        point absent at startup that never came back (see
        _expire_absent_at_startup). Caller must hold _em_lock, and must call
        this before _deindex_point (so _disable_entity_locked can still read
        the point's metadata for its stats bookkeeping).

        _reconcile_dynamic_points runs inside discover_points(), before
        scan_mqtt_discovery() has filled mqtt_enabled_points from the broker
        — so at startup that set is always empty here, and a check against it
        alone never fires. The retained discovery config of a point that went
        away while the bridge was down (its controlling switch changed on the
        controller itself, or a firmware update removed it) then survived:
        the scan found it, restore_from_mqtt() skipped it for having no
        metadata, and nothing ever cleared it — leaving a ghost entity in HA.

        When the point isn't known to be enabled, the domain it was published
        under isn't known either (nothing has been scanned yet), so every
        domain's config and attributes topic is cleared. An empty retained
        publish to a topic holding nothing is a no-op on the broker. These
        publishes go out before the scan subscribes on the same connection,
        so the scan no longer sees the config at all.
        """
        if point_id in self.mqtt_enabled_points:
            self._disable_entity_locked(point_id)
            return
        self._clear_discovery_topics(point_id)

    def _clear_discovery_topics(self, point_id: int) -> None:
        """Clear a point's config and attributes topic under every domain."""
        entity_id = create_entity_id(point_id)
        for domain in _DISCOVERY_DOMAINS:
            self.mqtt.publish(t_config(domain, entity_id), "", retain=True)
            self.mqtt.publish(t_attributes(domain, entity_id), "", retain=True)

    def _resend_dynamic_removals(self) -> None:
        """Clear the discovery config of every known dynamic point that has
        no entity now, again.

        The bridge removes an entity by clearing its retained config, and the
        broker passes that empty message on only to clients connected at that
        moment — it keeps nothing, since an empty retained message deletes the
        retained one. When Home Assistant's MQTT connection is down just then
        (it is reconnecting), it never hears of the removal, finds no config
        when it is back, and keeps the entity until it restarts. A dynamic
        point's entity comes and goes with its switch, so this is where that
        happens. Called once the startup action has restored the enabled set
        (for an entity left behind before the bridge restarted) and from
        republish_retained_state (Home Assistant's birth message, and every
        reconnect of the bridge). Clearing a topic that holds nothing is a
        no-op on the broker, and Home Assistant ignores the removal of an
        entity it doesn't have.

        Under _em_lock: a point appearing (_publish_dynamic_changes) holds it
        while it publishes its config, so its fresh config is never cleared.
        """
        if not self._enabled_set_restored:
            return
        with self._em_lock:
            with self._active_entities_lock:
                has_entity = set(self.active_entities_by_id) | self.mqtt_enabled_points
            gone = sorted(self.dynamic_point_map.all_known_dynamic_point_ids() - has_entity)
            for point_id in gone:
                self._clear_discovery_topics(point_id)
        if gone:
            # pragma: no mutate start
            log_discovery.info(
                "Resent the removal of %d dynamic point(s) without an entity: %s",
                len(gone),
                gone,
            )
            # pragma: no mutate end

    def _setup_dynamic_map_loading(self) -> None:
        """Subscribe to retained MQTT topics to restore dynamic state on startup.

        Two topics are loaded:
        1. DYNAMIC_MAP  — the full DynamicPointMap table.
        2. ACTIVE_DYNAMIC — the set of currently active dynamic point_ids.

        Both are only acted upon before initial_discovery_complete is True.
        After that point the in-memory state is authoritative and retained
        message re-deliveries (triggered by our own publishes) are ignored.
        """

        def on_dynamic_map_message(_client: Any, _userdata: Any, message: Any) -> None:
            if self.initial_discovery_complete:
                return
            try:
                if not message.payload:
                    return
                raw = message.payload.decode("utf-8")  # pragma: no mutate
                # Decompress if gzip-encoded
                if raw.startswith(_GZIP_SENTINEL):
                    # pragma: no mutate start
                    data = json.loads(
                        gzip.decompress(base64.b64decode(raw[len(_GZIP_SENTINEL) :])).decode(
                            "utf-8"
                        )
                    )
                    # pragma: no mutate end
                    json_str = json.dumps(data)
                else:
                    json_str = raw
                count = self.dynamic_point_map.deserialise(json_str)
                # pragma: no mutate start
                log_discovery.info("Restored DynamicPointMap from MQTT: %d entries", count)
                # pragma: no mutate end
            except Exception as e:  # noqa: BLE001 — best-effort restore from
                # external/retained MQTT data; must degrade gracefully no
                # matter what fails. Deliberately broad, not just decode/gzip/
                # JSON errors: self.dynamic_point_map.deserialise() is a call
                # into another object's method, not a fixed decode chain — a
                # test (test_dynamic_map_failure_logs_the_exception) mocks it
                # raising an arbitrary RuntimeError specifically to pin down
                # that this handler must not assume it knows deserialise()'s
                # full exception contract. A previous narrower except here
                # (UnicodeDecodeError/binascii.Error/OSError/EOFError/
                # zlib.error/json.JSONDecodeError only) let exactly that kind
                # of exception escape uncaught.
                # pragma: no mutate start
                log_discovery.warning(
                    "Could not restore DynamicPointMap from MQTT — "
                    "will try file fallback or start fresh: %s",
                    e,
                )
                # pragma: no mutate end

        def on_active_dynamic_message(_client: Any, _userdata: Any, message: Any) -> None:
            if self.initial_discovery_complete:
                return
            try:
                if not message.payload:
                    return
                point_ids = json.loads(message.payload.decode("utf-8"))  # pragma: no mutate
                if not isinstance(point_ids, list):
                    return
                loaded = {int(pid) for pid in point_ids}
                self.active_dynamic_points.update(loaded)
                # pragma: no mutate start
                log_discovery.info(
                    "Restored %d active dynamic point(s) from MQTT: %s",
                    len(loaded),
                    sorted(loaded),
                )
                # pragma: no mutate end
            except Exception as e:  # noqa: BLE001 — best-effort restore from
                # external/retained MQTT data; must degrade gracefully no
                # matter what fails. Deliberately broad, same reasoning as
                # on_dynamic_map_message above: this runs on paho's MQTT
                # network thread (suppress_exceptions=False), so any
                # exception that escapes here permanently kills the thread
                # and stops all future MQTT message delivery.
                # pragma: no mutate start
                log_discovery.warning("Could not restore active_dynamic_points from MQTT: %s", e)
                # pragma: no mutate end

        def on_wanted_points_message(_client: Any, _userdata: Any, message: Any) -> None:
            if self.initial_discovery_complete:
                return
            try:
                if not message.payload:
                    return
                point_ids = json.loads(message.payload.decode("utf-8"))  # pragma: no mutate
                if not isinstance(point_ids, list):
                    return
                loaded = {int(pid) for pid in point_ids}
                self._wanted_points.update(loaded)
                # pragma: no mutate start
                log_discovery.info(
                    "Restored %d wanted point(s) from MQTT: %s",
                    len(loaded),
                    sorted(loaded),
                )
                # pragma: no mutate end
            except Exception as e:  # noqa: BLE001 — best-effort restore from
                # external/retained MQTT data; same broad-catch reasoning as
                # on_active_dynamic_message above (runs on paho's network
                # thread with suppress_exceptions=False).
                # pragma: no mutate start
                log_discovery.warning("Could not restore wanted_points from MQTT: %s", e)
                # pragma: no mutate end

        def on_reclassified_points_message(_client: Any, _userdata: Any, message: Any) -> None:
            if self.initial_discovery_complete:
                return
            try:
                if not message.payload:
                    return
                point_ids = json.loads(message.payload.decode("utf-8"))  # pragma: no mutate
                if not isinstance(point_ids, list):
                    return
                self._binary_sensor_reclassified.update(int(pid) for pid in point_ids)
            except Exception as e:  # noqa: BLE001 — best-effort restore from
                # external/retained MQTT data; same broad-catch reasoning as
                # on_active_dynamic_message above (runs on paho's network
                # thread with suppress_exceptions=False).
                # pragma: no mutate start
                log_discovery.warning("Could not restore reclassified points from MQTT: %s", e)
                # pragma: no mutate end

        self._on_dynamic_map_message = on_dynamic_map_message
        self._on_active_dynamic_message = on_active_dynamic_message
        self._on_wanted_points_message = on_wanted_points_message
        self._on_reclassified_points_message = on_reclassified_points_message

        self.mqtt.subscribe(BrowserTopic.DYNAMIC_MAP)
        self.mqtt.message_callback_add(BrowserTopic.DYNAMIC_MAP, on_dynamic_map_message)
        self.mqtt.subscribe(BrowserTopic.ACTIVE_DYNAMIC)
        self.mqtt.message_callback_add(BrowserTopic.ACTIVE_DYNAMIC, on_active_dynamic_message)
        self.mqtt.subscribe(BrowserTopic.WANTED_POINTS)
        self.mqtt.message_callback_add(BrowserTopic.WANTED_POINTS, on_wanted_points_message)
        self.mqtt.subscribe(BrowserTopic.RECLASSIFIED_POINTS)
        self.mqtt.message_callback_add(
            BrowserTopic.RECLASSIFIED_POINTS, on_reclassified_points_message
        )

    def get_memory_usage(self) -> dict[str, Any]:
        """Return memory usage statistics for debugging and monitoring.

        Returns
        -------
        dict[str, Any]
            Dictionary with memory usage metrics including:
            - total_points: Total number of points tracked
            - active_entities: Number of active entities
            - cache_sizes: Sizes of various caches
            - estimated_memory_mb: Estimated memory usage in MB
        """

        stats: dict[str, Any] = {
            "total_points": len(self.all_points_by_id),
            "active_entities": len(self.active_entities_by_id),
            "mqtt_enabled_points": len(self.mqtt_enabled_points),
            "active_dynamic_points": len(self.active_dynamic_points),
            "last_states_size": len(self.last_states),
            "point_string_cache_size": len(self._point_string_cache),
            "pending_writes": len(self.pending_writes),
        }

        # Hit rate reveals whether the entity-type-detection cache is
        # actually earning its keep — a persistently low rate would mean
        # max_cache_size is too small for this installation's point count.
        cache_stats = self._point_string_cache.get_stats()
        stats["point_string_cache_hit_rate"] = round(cache_stats["hit_rate"], 3)

        estimated_bytes = (
            stats["total_points"] * 100
            + stats["active_entities"] * 500
            + sum(stats[f"{cache}_size"] for cache in ["last_states", "point_string_cache"]) * 50
        )
        stats["estimated_memory_mb"] = round(estimated_bytes / (1024 * 1024), 2)

        try:
            stats["actual_object_size_mb"] = round(sys.getsizeof(self) / (1024 * 1024), 2)
        except TypeError:
            # sys.getsizeof() raises TypeError if __sizeof__() returns a
            # non-int — not reachable for a normal instance, but this stat
            # is a nice-to-have diagnostic, not worth failing get_stats() over.
            stats["actual_object_size_mb"] = None

        return stats

    def _get_cached_entity_type(self, point_data: dict) -> tuple[str, str]:
        """Return the (entity_type, category) for a point, using a cache.

        Metadata and title are static within a single process run — a firmware
        update that changes them will restart the bridge, clearing the cache.
        The key is therefore just point_id; no complex compound key needed.
        """
        point_id = point_data["variableId"]
        cached: tuple[str, str] | None = self._entity_type_cache.get(point_id)
        if cached is not None:
            return cached
        result = detect_entity_type(point_data)
        if result[0] == "binary_sensor" and point_id in self._binary_sensor_reclassified:
            # Observed reporting a non-0/1 value at some point (see
            # _reclassify_binary_sensor) — static metadata would make it a
            # binary_sensor again, and republishing it as one deletes the
            # sensor entity in HA only for the next poll to recreate it.
            result = ("sensor", result[1])
        self._entity_type_cache.put(point_id, result)
        return result

    def _check_memory_and_cleanup(self) -> None:
        """No-op hook for periodic memory housekeeping.

        ``_point_string_cache`` (LRUCache) self-evicts the least-recently-used
        entry on every ``put()`` call once ``max_size`` is reached, so no
        manual intervention is needed.  This method is retained as a call-site
        placeholder for any future housekeeping that cannot be done inline.
        """

    # ------------------------------------------------------------------ #
    # Snapshots                                                            #
    # ------------------------------------------------------------------ #

    def _load_snapshots(self, path: str | None = None) -> list[dict]:
        """Load snapshots from /data/snapshots.json. Returns [] on any error."""
        if path is None:
            path = _SNAPSHOTS_FILE
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return []

    def _save_snapshots(self, snapshots: list[dict], path: str | None = None) -> None:
        """Persist snapshots to /data/snapshots.json and publish to MQTT."""
        if path is None:
            path = _SNAPSHOTS_FILE
        try:
            atomic_write_text(path, json.dumps(snapshots, indent=2))
        except OSError as e:
            log_restore.warning("Could not write snapshots file: %s", e)  # pragma: no mutate
        payload = json.dumps(snapshots)
        self.mqtt.publish(BrowserTopic.SNAPSHOTS, payload, retain=True)

    def save_snapshot(self, name: str, path: str | None = None) -> tuple[bool, str]:
        """Save the current enabled entity set as a named snapshot.

        Parameters
        ----------
        name:
            Display name for the snapshot. Must be non-empty after stripping.

        Returns
        -------
        (success, message) — message describes the result for the card UI.
        """
        name = name.strip()
        if not name:
            return False, "Snapshot name must not be empty."

        # _em_lock: mqtt_enabled_points is mutated by _enable_entity_locked/
        # _disable_entity_locked on other mgmt_executor worker threads (the
        # executor has multiple workers) — read it under the same lock those
        # mutators hold, and snapshot it once so the point_ids list and
        # point_count below can't disagree if a concurrent enable/disable
        # lands between two separate reads of the live set.
        with self._em_lock:
            enabled_snapshot = sorted(self.mqtt_enabled_points)

        # _snapshots_lock: serializes this load-modify-save sequence against
        # concurrent save_snapshot/delete_snapshot calls — see its
        # declaration in __init__ for why an unguarded sequence is unsafe.
        with self._snapshots_lock:
            snapshots = self._load_snapshots(path=path)

            # Replace existing snapshot with the same name
            snapshots = [s for s in snapshots if s.get("name") != name]

            if len(snapshots) >= _SNAPSHOTS_MAX:
                return False, (
                    f"Maximum of {_SNAPSHOTS_MAX} snapshots reached. "
                    "Delete one before saving a new snapshot."
                )

            snapshot = {
                "name": name,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "point_ids": enabled_snapshot,
                "point_count": len(enabled_snapshot),
                # The mode the bridge knows it applied first: the /data file
                # alone can be missing (it doesn't survive a container rebuild
                # everywhere), which saved the snapshot as mode "unknown".
                "mode": self._applied_mode_known
                or self._read_applied_mode_from_file()
                or "unknown",
            }
            snapshots.append(snapshot)
            self._save_snapshots(snapshots, path=path)
        # pragma: no mutate start
        log_restore.info("Snapshot '%s' saved: %d points", name, len(enabled_snapshot))
        # pragma: no mutate end
        return True, f"Snapshot '{name}' saved ({len(enabled_snapshot)} points)."

    def restore_snapshot(
        self, name: str, mode: str = "flush", path: str | None = None
    ) -> tuple[bool, str]:
        """Restore a named snapshot.

        Parameters
        ----------
        name:
            Name of the snapshot to restore.
        mode:
            'flush' — disable all current entities then enable the saved set.
            'merge' — keep current entities and additionally enable the saved set.

        Returns
        -------
        (success, message)
        """
        # _snapshots_lock: see save_snapshot's declaration in __init__ —
        # without it, this read can race a concurrent save_snapshot/
        # delete_snapshot's write (mgmt_executor has multiple workers), get
        # a torn/partial JSON read, and silently report "not found" for a
        # snapshot that genuinely exists on disk.
        with self._snapshots_lock:
            snapshots = self._load_snapshots(path=path)
        snapshot = next((s for s in snapshots if s.get("name") == name), None)
        if snapshot is None:
            return False, f"Snapshot '{name}' not found."

        # Restoring into menus or all mode would conflict with the system-managed
        # entity set — the mode re-applies on restart and overwrites the restored
        # selection. Block restore and ask the user to switch to a manual mode first.
        # The mode the bridge knows it applied first — the /data file alone can
        # be missing (see save_snapshot).
        current_mode = self._applied_mode_known or self._read_applied_mode_from_file()
        if current_mode in ("menus", "all"):
            return False, (
                f"Cannot restore a snapshot while in '{current_mode}' mode. "
                "Switch to 'essential', 'monitoring', 'advanced', or 'none' first, "
                "then restore."
            )

        saved_ids = set(snapshot.get("point_ids", []))
        firmware_ids = set(self.all_points_by_id.keys())
        valid_ids = saved_ids & firmware_ids
        missing = saved_ids - firmware_ids

        if missing:
            # pragma: no mutate start
            log_restore.info(
                "Snapshot '%s': %d point(s) no longer in firmware — skipped: %s",
                name,
                len(missing),
                sorted(missing)[:10],
            )
            # pragma: no mutate end

        protected = set(self.active_dynamic_points)

        with self._em_lock:
            with self._suppress_lock:
                was_suppressed = self._suppress_enabled_state_depth > 0
                if not was_suppressed:
                    self._suppress_enabled_state_depth += 1  # pragma: no mutate

            try:
                if mode == "flush":
                    # Disable everything not in the snapshot (protect dynamic points)
                    to_disable = (self.mqtt_enabled_points - valid_ids) - protected
                    for pid in to_disable:
                        self._disable_entity_locked(pid)
                        # Snapshot-driven disable is intentional user override —
                        # same reasoning as apply_mode's to_disable loop.
                        self._unmark_wanted(pid)
                    # And points the snapshot excludes that are wanted but not
                    # enabled (see _excluded_wanted_points).
                    for pid in self._excluded_wanted_points(valid_ids, protected):
                        self._unmark_wanted(pid)

                # Enable all valid snapshot points not already enabled
                to_enable = valid_ids - self.mqtt_enabled_points
                for pid in to_enable:
                    if self._enable_entity_locked(pid):
                        self._mark_wanted(pid)
            finally:
                if not was_suppressed:
                    with self._suppress_lock:
                        self._suppress_enabled_state_depth -= 1

        self.publish_enabled_state()

        msg = (
            f"Snapshot '{name}' restored ({len(valid_ids)} points"
            + (f", {len(missing)} skipped — not in firmware" if missing else "")
            + ")."
        )
        # pragma: no mutate start
        log_restore.info("Snapshot '%s' restored (mode=%s): %s", name, mode, msg)
        # pragma: no mutate end
        return True, msg

    def delete_snapshot(self, name: str, path: str | None = None) -> tuple[bool, str]:
        """Delete a named snapshot."""
        # _snapshots_lock: see save_snapshot — serializes this load-modify-
        # save sequence against concurrent save_snapshot/delete_snapshot calls.
        with self._snapshots_lock:
            snapshots = self._load_snapshots(path=path)
            filtered = [s for s in snapshots if s.get("name") != name]
            if len(filtered) == len(snapshots):
                return False, f"Snapshot '{name}' not found."
            self._save_snapshots(filtered, path=path)
        log_restore.info("Snapshot '%s' deleted", name)  # pragma: no mutate
        return True, f"Snapshot '{name}' deleted."

    def publish_snapshots(self) -> None:
        """Publish current snapshot list to the MQTT browser topic."""
        snapshots = self._load_snapshots()
        self.mqtt.publish(BrowserTopic.SNAPSHOTS, json.dumps(snapshots), retain=True)


# ============================================================================
# HELPER — build device_info dict from API response
# ============================================================================


def _build_device_info(
    api_response: dict,
    device_id: str,
    device_name: str,
    base_url: str,
) -> dict:
    """Build the HA MQTT device object from the Nibe API response.

    Device name priority:
      1. ``device_name`` from config — always honoured if the user set it
         to anything other than the default.
      2. ``product.name`` from API response — used when the config name is
         still the default and the API provides a non-empty name.
      3. ``"Nibe S-series"`` — fallback when both are empty or default.

    The model field always follows the API, falling back to S-series.
    This way the HA device card shows the correct model regardless of
    what the user named the device.
    """
    product = api_response.get("product", {})
    api_name = (product.get("name") or "").strip()
    model_name = api_name or "Nibe S-series"

    # Use the config device_name as-is — it is always the user's explicit
    # choice.  If the API provides a name and the config is still the
    # shipped default, prefer the API name so it reflects the actual
    # hardware without requiring manual configuration.
    _DEFAULT_DEVICE_NAME = "Nibe SMO S40"
    resolved_name = api_name if device_name == _DEFAULT_DEVICE_NAME and api_name else device_name

    device_root = base_url.rsplit("/api/v1/devices/", 1)[0]
    device = {
        "identifiers": [device_id],
        "name": resolved_name,
        "manufacturer": product.get("manufacturer", "NIBE"),
        "model": model_name,
        "model_id": product.get("firmwareId", ""),
        "serial_number": product.get("serialNumber", ""),
        "configuration_url": device_root,
    }
    return {k: v for k, v in device.items() if v != ""}
