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
- Value cache: suppressing redundant MQTT publishes via change-threshold
  and minimum-interval guards (ValueCache).

What this module does NOT do
-----------------------------
- No raw HTTP (all API calls go through NibeApiClient).
- No topic string construction (all MQTT publishing through MqttDiscoveryPublisher).
- No HA registry watching or notification sending.

Public surface
--------------
ValueCache
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
import gzip
import json
import logging
import sys
import threading
import time
import urllib.error
import uuid
from collections import deque, OrderedDict
from collections.abc import Callable, Generator
from contextlib import contextmanager

from nibe_utils import fmt_ts as _fmt_ts
from nibe_dynamic_map import DynamicPointEntry, DynamicPointMap
from nibe_entity_detection import (
    MODES,
    clean_string,
    detect_entity_type,
    get_register_type,
    get_value_mapping,
    apply_divisor,
    reverse_divisor,
)
from nibe_mqtt_publisher import MqttDiscoveryPublisher, BrowserTopic

log_api       = logging.getLogger("nibe.api")
log_mqtt      = logging.getLogger("nibe.mqtt")
log_discovery = logging.getLogger("nibe.discovery")
log_restore   = logging.getLogger("nibe.restore")
log_entities  = logging.getLogger("nibe.entities")
log_commands  = logging.getLogger("nibe.commands")
log_history   = logging.getLogger("nibe.history")

# ── Module-level constants ─────────────────────────────────────────────────────
# Keeping all tuneable numbers here (rather than inline) makes it trivial to
# audit limits, and eliminates "what does 60 mean here?" questions in code review.

# Write commands
_CMD_ID_LENGTH          = 8     # hex chars in a correlation ID (uuid4 prefix)
_TEXT_REGISTER_MAX_LEN  = 64    # max chars for a Nibe string register

# Pending-write staleness: entries older than this are treated as timed-out
_STALE_WRITE_AGE_S      = 60    # seconds

# Post-write dynamic-point scan window
_POST_WRITE_SCAN_S      = 90    # seconds to keep accelerated bulk polling
                                # Nibe firmware has ~60s internal cache refresh
                                # cycle; 90s gives comfortable margin.

# Changelog
_CHANGELOG_MAX_ENTRIES  = 500   # hard deque cap; time-based prune also runs
_CHANGELOG_MIN_ENTRIES  = 50    # always keep at least this many entries
_CHANGELOG_PRUNE_S      = 3600  # run time-based prune at most once per hour

# MQTT scan sentinel
_MQTT_SCAN_TIMEOUT_S    = 15    # seconds to wait for the sentinel round-trip

# Applied-mode persistence (entity mode reconciliation across restarts)
_APPLIED_MODE_FILE      = '/data/applied_mode'
_SNAPSHOTS_FILE         = '/data/snapshots.json'
_SNAPSHOTS_MAX          = 10   # maximum number of named snapshots
_APPLIED_MODE_TIMEOUT_S = 5     # seconds to wait for the retained applied_mode
                                # message — a single topic, not a full scan, so
                                # far shorter than _MQTT_SCAN_TIMEOUT_S. Only
                                # ever times out on the very first startup after
                                # this topic exists (never published before);
                                # every subsequent startup gets it retained.

# HA persistent notification IDs — centralised so dismissals and raises
# always use the same string and a typo can't leave a ghost notification.
_NOTIF_API_UNREACHABLE      = "nibe_api_unreachable"
_NOTIF_WRITE_ERROR          = "nibe_write_error"
_NOTIF_NO_ENTITIES          = "nibe_no_entities"
_NOTIF_DISCOVERY_INCOMPLETE = "nibe_discovery_incomplete"



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
    raw        = json.dumps(data, separators=(',', ':')).encode('utf-8')
    compressed = gzip.compress(raw, compresslevel=6)
    return _GZIP_SENTINEL + base64.b64encode(compressed).decode('ascii')

def _decompress_payload(payload: bytes | str) -> bytes:
    """Decompress a payload produced by ``_compress_payload``.

    Accepts either bytes (from paho's message.payload) or str.
    """
    if isinstance(payload, (bytes, bytearray)):
        text = payload.decode('utf-8', errors='replace')
    else:
        text = payload

    compressed = base64.b64decode(text[len(_GZIP_SENTINEL):])
    return gzip.decompress(compressed)

# ============================================================================
# VALUE CACHE
# ============================================================================

class ValueCache:
    """Rate-limits MQTT state publishes via a change-threshold and minimum interval.

    Without this cache every bulk fetch would republish every entity on every
    poll cycle regardless of whether the value changed, producing unnecessary
    MQTT traffic and HA history entries.

    _cache stores the last-published raw integer value per point_id.
    _last_publish stores the timestamp of the last publish per point_id.
    These are kept separate so _cache stays a simple int lookup.
    """
    __slots__ = ('_cache', '_last_publish', '_lock')

    def __init__(self):
        self._cache        = {}   # point_id → last published raw int value
        self._last_publish = {}   # point_id → timestamp of last publish
        self._lock         = threading.Lock()

    def should_publish(
        self,
        point_id: int,
        raw_value: int,
        threshold: int,
        force: bool = False,
        min_interval: int = 30,
    ) -> bool:
        """Return True if the value warrants publishing to MQTT."""
        current_time = time.time()
        with self._lock:
            if force or point_id not in self._cache:
                self._cache[point_id]        = raw_value
                self._last_publish[point_id] = current_time
                return True

            if point_id in self._last_publish:
                if current_time - self._last_publish[point_id] < min_interval and not force:
                    return False

            old_value = self._cache[point_id]
            if abs(raw_value - old_value) >= threshold:
                self._cache[point_id]        = raw_value
                self._last_publish[point_id] = current_time
                return True
        return False

    def update(self, point_id: int, raw_value: int) -> None:
        """Update cached value without triggering a publish decision."""
        with self._lock:
            self._cache[point_id] = raw_value

    def discard(self, point_id: int) -> None:
        """Remove all cached state for a point (called on entity disable)."""
        with self._lock:
            self._cache.pop(point_id, None)
            self._last_publish.pop(point_id, None)


class LRUCache:
    """Memory-efficient LRU (Least Recently Used) cache with automatic cleanup.
    
    This cache automatically removes least recently used items when the cache
    exceeds its maximum size, making it ideal for caching data where some
    items are more important than others.
    
    Parameters
    ----------
    max_size : int
        Maximum number of items to keep in cache. When exceeded, LRU items are removed.
    """
    
    def __init__(self, max_size: int = 1000):
        self.max_size = max_size
        self._cache: OrderedDict = OrderedDict()  # OrderedDict maintains insertion order
        self._hits = 0
        self._misses = 0
    
    def get(self, key):
        """Get item from cache, marking it as recently used."""
        try:
            value = self._cache.pop(key)
            self._cache[key] = value  # Move to end (most recently used)
            self._hits += 1
            return value
        except KeyError:
            self._misses += 1
            return None
    
    def put(self, key, value):
        """Add item to cache, removing LRU item if max_size exceeded."""
        if key in self._cache:
            # Update existing item - move to end
            self._cache.pop(key)
        elif len(self._cache) >= self.max_size:
            # Remove least recently used item
            self._cache.popitem(last=False)
        
        self._cache[key] = value
    
    def __contains__(self, key):
        return key in self._cache
    
    def __len__(self):
        return len(self._cache)
    
    def pop(self, key, default=None):
        """Remove and return item from cache. Compatible with dict.pop()."""
        if key in self._cache:
            value = self._cache.pop(key)
            return value
        return default
    
    def __getitem__(self, key):
        """Support dict-like access: cache[key]. Promotes to MRU and counts as a hit."""
        value = self._cache.pop(key)        # raises KeyError if absent — correct
        self._cache[key] = value            # re-insert at end (most recently used)
        self._hits += 1
        return value
    
    def clear(self):
        """Clear the cache."""
        self._cache.clear()
        self._hits = 0
        self._misses = 0
    
    def get_stats(self):
        """Return cache statistics."""
        return {
            'size': len(self._cache),
            'capacity': self.max_size,
            'hit_rate': self._hits / (self._hits + self._misses) if (self._hits + self._misses) > 0 else 0,
            'hits': self._hits,
            'misses': self._misses
        }


# ============================================================================
# STARTUP MODE DECISION (pure — unit-testable without mocking main())
# ============================================================================

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
        api_client,
        publisher: MqttDiscoveryPublisher,
        notify_fn: Callable,
        dismiss_fn: Callable,
        mqtt_client,
        max_cache_size: int = 5000,
    ) -> None:
        self._api      = api_client
        self._pub      = publisher
        self._notify   = notify_fn
        self._dismiss  = dismiss_fn
        self.mqtt      = mqtt_client

        # Performance optimization: Cache entity type detection results.
        # Metadata, title, and description are static within a single run
        # (firmware updates restart the bridge) so point_id alone is the key.
        self._entity_type_cache: LRUCache = LRUCache(max_size=2000)

        # ── Point registry ────────────────────────────────────────────────────
        # Single source of truth: dict keyed by variableId.
        # .all_points property exposes list(values()) for callers that need a list.
        self.all_points_by_id: dict[int, dict] = {}

        # ── Active entity registry ────────────────────────────────────────────
        # Dict keyed by point_id; .active_entities property wraps it as a list.
        self.active_entities_by_id: dict[int, dict] = {}
        self._active_entities_lock = threading.Lock()

        # ── Enabled-state tracking ────────────────────────────────────────────
        self.mqtt_enabled_points: set[int] = set()

        # ── Dynamic point tracking ────────────────────────────────────────────
        # DynamicPointMap replaces the former flat known_dynamic_points set.
        # It records, for every writable switch/select, which values cause
        # dynamic points to appear/disappear in the firmware's bulk fetch.
        # Loaded from MQTT (primary) or /data/dynamic_point_map.json (fallback)
        # at startup; persisted write-through on every change.
        self.dynamic_point_map: DynamicPointMap = DynamicPointMap()

        # Reverse lookup: point_id → (menu_id, menu_title) built from
        # menu_structure.yaml at dashboard provisioning time. Used to include
        # menu location in dynamic change notifications.
        self.point_to_menu_map: dict[int, tuple[str, str]] = {}
        # Currently active dynamic point_ids — the set that should be enabled
        # in HA right now.  Persisted to MQTT (ACTIVE_DYNAMIC topic) so restarts
        # resume the correct active set without re-running discovery.
        self.active_dynamic_points: set[int] = set()
        self.baseline_point_ids:   set[int] = set()
        self.initial_discovery_complete: bool = False

        # ── Write metrics (Finding 3) ─────────────────────────────────────────
        # Monotonically increasing counters for the lifetime of this bridge session.
        # Published as part of the stats payload and the bridge/status topic so
        # automations and external monitors can track write health over time.
        self._write_total:   int = 0
        self._write_success: int = 0
        self._write_failed:  int = 0
        self._last_write_error: str | None = None   # human-readable last error
        self.pending_writes: dict[int, dict]  = {}
        self._pending_writes_lock = threading.Lock()
        self._write_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="nibe_write"
        )

        # ── Learning mode ─────────────────────────────────────────────────────
        # When True, writes to unprocessed switches/selects are serialised and
        # ── Post-write scan ───────────────────────────────────────────────────
        # After any write to a switch, _post_write_active is set to True and
        # _post_write_until records when the accelerated scan window ends.
        # During this window the main poll loop uses _post_write_interval
        # instead of bulk_interval so dynamic point changes surface quickly.
        self._post_write_active:             bool  = False
        self._post_write_until:              float = 0.0
        self._post_write_interval:           int   = 5
        self._post_write_duration:           int   = _POST_WRITE_SCAN_S
        self._post_write_controlling_point:  int | None = None
        # Points confirmed present by _resolve_dynamic_points_fast, keyed to
        # expiry time.  The firmware bulk API has a ~60s cache, so the next
        # bulk fetch may return stale data that omits the newly-activated point.
        # Entries here suppress false-disappearance routing until the bulk cache
        # clears or the point actually appears in the bulk response.

        # ── Bulk data cache ───────────────────────────────────────────────────
        self.bulk_data: dict[int, dict] = {}

        # Cache for cleaned title/description strings.  The Nibe API never
        # changes these between firmware updates, so running clean_string on
        # every point every poll is wasted work.  Keyed by point_id; value is
        # (raw_title, raw_description, cleaned_title, cleaned_description).
        # Invalidated automatically when the raw string from the API changes.
        self._point_string_cache = LRUCache(max_size=max_cache_size)

        # ── Misc state ────────────────────────────────────────────────────────
        self.last_states:         dict[int, str]   = {}
        self.value_cache          = ValueCache()
        self.last_bulk_fetch:     float = 0
        self.bulk_interval:       int   = 30
        self.api_failure_threshold: int = 3
        self.last_fetch_duration: float = 0.0
        self.bridge_start_time:   float = time.time()
        self._on_enabled_state_change = None
        self._last_published_enabled: frozenset[int] = frozenset()
        self.api_consecutive_failures: int   = 0
        self.api_last_success_time:    float = time.time()
        self._bulk_fetch_lock     = threading.Lock()
        self.published_configs:   set[int] = set()

        # ── Device mode cache ─────────────────────────────────────────────────
        # Avoids a redundant fetch_device_info() call on every poll cycle just
        # to read aidMode and smartMode.  Set dirty=True after a write so the
        # next poll refreshes from the API.
        self.device_modes_cache: dict[str, str] = {}
        self.device_modes_dirty: bool = True

        # ── Notification flags ────────────────────────────────────────────────
        self._api_notification_active:       bool = False
        self._alarm_notification_active:     bool = False
        self._last_alarm_count:              int  = -1   # -1 forces first log
        self._last_stats_key:                tuple | None = None
        self._write_notification_active:     bool = False
        self._discovery_notification_active: bool = False
        self._range_warnings_issued:         set[int] = set()

        # ── Incremental stats counters ────────────────────────────────────────
        self._stats_type_counts:     dict[str, int] = {}
        self._stats_category_counts: dict[str, int] = {}
        self._stats_writable_count:  int = 0

        # ── Enabled-state publish suppression ─────────────────────────────────
        self._suppress_enabled_state_depth = 0
        self._suppress_lock = threading.Lock()

        # ── Changelog ─────────────────────────────────────────────────────────
        # deque with a hard cap prevents unbounded growth even if _prune_changelog
        # is somehow skipped.  appendleft() is O(1) vs list.insert(0, …) O(n).
        # Time-based pruning still runs on startup and periodically (hourly) to
        # evict entries older than changelog_retention_days.
        self.change_history:          deque = deque(maxlen=_CHANGELOG_MAX_ENTRIES)
        self.changelog_retention_days: int  = 90
        self._history_seq:            int   = 0
        self._last_published_seq:     int   = 0
        self._last_prune_time:        float = 0.0   # for hourly prune cadence

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

        self._setup_history_loading()
        self._setup_dynamic_map_loading()

    # ------------------------------------------------------------------ #
    # Public list views                                                    #
    # ------------------------------------------------------------------ #

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
        self.all_points_by_id[point['variableId']] = point

    def _deindex_point(self, point_id: int) -> None:
        self.all_points_by_id.pop(point_id, None)
        self._point_string_cache.pop(point_id, None)

    # ------------------------------------------------------------------ #
    # Discovery                                                            #
    # ------------------------------------------------------------------ #

    def discover_points(self) -> bool:
        """Fetch all points from the API and establish the static baseline.

        The baseline is the set of points present at startup.  Subsequent
        bulk fetches compare against it to identify dynamic points.

        After establishing the baseline:
        1. Populates DynamicPointMap with any writable switches/selects not
           yet in the table (first run: all; subsequent: incremental).
        2. Reconciles dynamic point state against persisted active set.
        """
        log_discovery.info("Initial point discovery...")

        # If the DynamicPointMap is empty after MQTT loading, try file fallback
        if len(self.dynamic_point_map) == 0:
            file_count = self.dynamic_point_map.from_file()
            if file_count:
                log_discovery.info(
                    "DynamicPointMap loaded from file fallback: %d entries", file_count
                )

        if not self._fetch_bulk_data(detect_changes=False):
            log_discovery.error("Initial discovery failed")
            return False

        self.baseline_point_ids = set(p['variableId'] for p in self.all_points)
        self.initial_discovery_complete = True
        log_discovery.info("Baseline established: %d static points", len(self.baseline_point_ids))

        # Resolve entity types for all indexed points so populate_from_bulk
        # can filter to switches and selects only.
        entity_types = {
            pid: pt.get('entity_type', '')
            for pid, pt in self.all_points_by_id.items()
        }
        added = self.dynamic_point_map.populate_from_bulk(
            self.all_points_by_id, entity_types
        )
        if added:
            log_discovery.info(
                "DynamicPointMap: populated %d new skeleton entries", added
            )
            self._persist_dynamic_map()

        # Restore firmware_removed status for any entries that reappeared
        self.dynamic_point_map.restore_from_bulk(self.baseline_point_ids)

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
        log_discovery.info("API is back — completing deferred startup discovery...")

        device_response = self._api.fetch_device_info()
        if device_response:
            self.device_info = _build_device_info(
                device_response, self._pub.device_id, self._pub.device_name,
                self._api.base_url
            )
            self._pub.device_info = self.device_info
            log_discovery.info(
                "Device info updated: model=%s, serial=%s",
                self.device_info.get("model"),
                self.device_info.get("serial_number"),
            )
        else:
            log_discovery.warning("Could not fetch device info — serial/firmware will be blank")

        if not self.discover_points():
            log_discovery.warning("Deferred discovery fetch failed — will retry next poll")
            return False

        mqtt_enabled = self.scan_mqtt_discovery()
        applied_mode = self.read_applied_mode() if mqtt_enabled else None
        action = decide_startup_action(
            has_existing_entities=bool(mqtt_enabled),
            applied_mode=applied_mode,
            config_mode=initial_mode,
        )

        if action == "apply":
            log_restore.warning(
                "Deferred MQTT scan found 0 existing discovery configs — "
                "looks like a fresh install. Applying mode '%s'.",
                initial_mode,
            )
        elif action == "restore":
            log_restore.info("Restoring entities from MQTT database...")
        else:
            log_restore.info(
                "Deferred discovery: applied mode differs from configured mode '%s' — "
                "restoring then reconciling to the new mode.",
                initial_mode,
            )

        self._apply_startup_action(action, applied_mode, initial_mode)

        self.publish_enabled_state()
        log_discovery.info(
            "Deferred discovery complete: %d points, %d enabled, %d active",
            len(self.all_points), len(self.mqtt_enabled_points), len(self.active_entities),
        )
        return True

    def _apply_startup_action(
        self,
        action:       str,
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
                log_restore.info(
                    "No applied-mode record found — establishing baseline at "
                    "current mode '%s' (existing entities left unchanged).",
                    initial_mode,
                )
                self.record_applied_mode(initial_mode)
        else:  # "reconcile"
            self.restore_from_mqtt()
            self.apply_mode(initial_mode)

    # ------------------------------------------------------------------ #
    # MQTT scan / restore                                                  #
    # ------------------------------------------------------------------ #

    def scan_mqtt_discovery(self) -> set[int]:
        """Scan the broker for retained HA discovery configs.

        Uses a sentinel message to detect end-of-retained-messages reliably
        rather than a fixed sleep, with a hard timeout fallback.
        """
        _SENTINEL_TOPIC = BrowserTopic.SCAN_SENTINEL

        log_discovery.debug("Scanning MQTT for existing discovery configs...")
        discovered_points: set[int] = set()
        sentinel_received = threading.Event()

        def on_config(_client, _userdata, message):
            topic = message.topic
            if (topic.startswith("homeassistant/") and topic.endswith("/config")
                    and message.payload):
                try:
                    config    = json.loads(message.payload.decode('utf-8'))
                    unique_id = config.get('unique_id', '')
                    if unique_id.startswith('nibe_'):
                        id_str = unique_id[5:]
                        if id_str.isdigit():
                            discovered_points.add(int(id_str))
                except (json.JSONDecodeError, KeyError, ValueError) as e:
                    log_discovery.warning("Could not parse config from %s: %s", topic, e)

        def on_sentinel(_client, _userdata, _message):
            sentinel_received.set()

        config_topic = "homeassistant/+/+/config"
        self.mqtt.subscribe(config_topic)
        self.mqtt.message_callback_add(config_topic, on_config)
        self.mqtt.subscribe(_SENTINEL_TOPIC)
        self.mqtt.message_callback_add(_SENTINEL_TOPIC, on_sentinel)
        self.mqtt.publish(_SENTINEL_TOPIC, "scan", retain=False)

        if not sentinel_received.wait(timeout=_MQTT_SCAN_TIMEOUT_S):
            log_discovery.warning(
                "Sentinel timeout after %ds — retained message delivery may be incomplete",
                _MQTT_SCAN_TIMEOUT_S,
            )

        self.mqtt.message_callback_remove(config_topic)
        self.mqtt.message_callback_remove(_SENTINEL_TOPIC)
        self.mqtt.unsubscribe(config_topic)
        self.mqtt.unsubscribe(_SENTINEL_TOPIC)

        self.mqtt_enabled_points.clear()
        self.mqtt_enabled_points.update(discovered_points)
        log_discovery.debug("Found %d existing MQTT discovery configs", len(discovered_points))
        return discovered_points

    def restore_from_mqtt(self) -> int:
        """Rebuild active_entities from the set found by scan_mqtt_discovery().

        Re-publishes every discovery config so code changes take effect on
        restart without requiring a manual disable/re-enable cycle.
        Only republishes configs whose hash has changed since the last run,
        reducing MQTT traffic on restarts where nothing has changed.
        """
        if not self.mqtt_enabled_points:
            log_restore.info("No existing MQTT configs found")
            return 0

        log_restore.info("Restoring %d entities from MQTT...", len(self.mqtt_enabled_points))
        restored_count  = 0
        republished     = 0
        failed_points   = []

        for point_id in self.mqtt_enabled_points:
            point = self.all_points_by_id.get(point_id)
            if not point:
                log_restore.warning(
                    "Point %d is in the enabled list but has no metadata — "
                    "it may be a dynamic point not currently active, "
                    "or it was removed in a firmware update. Skipping restore.",
                    point_id,
                )
                failed_points.append(point_id)
                continue

            entity_info = self._pub.publish_entity_discovery(
                point, self.bulk_data
            )
            if entity_info:
                with self._active_entities_lock:
                    prev = self.active_entities_by_id.get(point_id)
                    self.active_entities_by_id[point_id] = entity_info
                restored_count += 1
                if prev is None:
                    republished += 1

                if point.get('is_dynamic', False):
                    self.active_dynamic_points.add(point_id)

                if entity_info['command_topic']:
                    def _make_handler(ei):
                        def handler(_client, _userdata, message):
                            self._handle_command(ei, message)
                        return handler
                    self.mqtt.subscribe(entity_info['command_topic'], qos=1)
                    self.mqtt.message_callback_add(
                        entity_info['command_topic'], _make_handler(entity_info)
                    )

                # Publish online for all restored entities so HA doesn't
                # show them as unavailable on startup. State will be updated
                # on the first bulk fetch poll.
                self.mqtt.publish(entity_info['availability_topic'], "online", retain=True)

            else:
                failed_points.append(point_id)

        if failed_points:
            log_restore.warning("Failed to restore %d points", len(failed_points))
            for point_id in failed_points:
                self.mqtt_enabled_points.discard(point_id)

        log_restore.info(
            "Restored %d/%d entities (%d configs republished)",
            restored_count, len(self.mqtt_enabled_points), republished,
        )

        return restored_count

    # ------------------------------------------------------------------ #
    # Enable / disable                                                     #
    # ------------------------------------------------------------------ #

    def enable_entity(self, point_id: int) -> bool:
        """Publish an MQTT discovery config for a point, making it visible in HA."""
        point = self.all_points_by_id.get(point_id)
        if not point:
            log_entities.warning(
                "Cannot enable point %d: not in bulk data "
                "(conditional point absent for this firmware/accessory configuration?)",
                point_id,
            )
            return False

        if point_id in self.mqtt_enabled_points:
            log_entities.debug(
                "Point %d is already enabled (discovery config exists) — skipping",
                point_id,
            )
            return True

        entity_info = self._pub.publish_entity_discovery(point, self.bulk_data)
        if not entity_info:
            return False

        with self._active_entities_lock:
            self.active_entities_by_id[point_id] = entity_info
        self.mqtt_enabled_points.add(point_id)

        if entity_info['command_topic']:
            def command_handler(_client, _userdata, message):
                self._handle_command(entity_info, message)
            self.mqtt.subscribe(entity_info['command_topic'], qos=1)
            self.mqtt.message_callback_add(entity_info['command_topic'], command_handler)

        # Update incremental stats
        entity_type_key = point.get('entity_type', 'unknown')
        category_key    = point.get('entity_category', 'none')
        self._stats_type_counts[entity_type_key] = (
            self._stats_type_counts.get(entity_type_key, 0) + 1
        )
        self._stats_category_counts[category_key] = (
            self._stats_category_counts.get(category_key, 0) + 1
        )
        if point.get('is_writable', False):
            self._stats_writable_count += 1

        self.mqtt.publish(entity_info['availability_topic'], "online", retain=True)
        # Only call _update_entity_state if bulk data is available for this point.
        # If it isn't (e.g. first enable before the first successful API poll),
        # skip the state publish here — the next poll will handle it.
        # Calling _update_entity_state when the point is absent from bulk_data
        # triggers its auto-disable path, immediately undoing the enable.
        if entity_info['point_id'] in self.bulk_data:
            self._update_entity_state(entity_info)

        if not self._is_suppressed():
            self.publish_enabled_state()

        # Dismiss the "no entities enabled" notification on the first successful enable.
        if self.mqtt and len(self.mqtt_enabled_points) == 1:
            self._dismiss(self.mqtt, _NOTIF_NO_ENTITIES)

        log_entities.info("Enabled point %d", point_id)
        return True

    def disable_entity(self, point_id: int) -> bool:
        """Remove the MQTT discovery config for a point, hiding it from HA."""
        if point_id not in self.mqtt_enabled_points:
            return True

        with self._active_entities_lock:
            entity_info = self.active_entities_by_id.pop(point_id, None)

        if entity_info:
            from nibe_mqtt_publisher import t_config
            config_topic = t_config(entity_info['entity_type'], entity_info['entity_id'])
            self.mqtt.publish(config_topic, "", retain=True)
            # Invalidate the cached config hash so the next enable unconditionally
            # republishes the discovery config.  HA removes the entity when the
            # config is cleared, so the hash is stale and must not suppress the
            # next publish.
            self._pub.invalidate_config_hash(point_id)

            if entity_info.get('attributes_topic'):
                self.mqtt.publish(entity_info['attributes_topic'], "", retain=True)

            if entity_info.get('command_topic'):
                self.mqtt.message_callback_remove(entity_info['command_topic'])
                self.mqtt.unsubscribe(entity_info['command_topic'])

        self.mqtt_enabled_points.discard(point_id)
        self.last_states.pop(point_id, None)
        self.value_cache.discard(point_id)
        self._point_string_cache.pop(point_id, None)
        self._entity_type_cache.pop(point_id, None)

        point           = self.all_points_by_id.get(point_id, {})
        entity_type_key = point.get('entity_type', 'unknown')
        category_key    = point.get('entity_category', 'none')
        if entity_type_key in self._stats_type_counts:
            self._stats_type_counts[entity_type_key] = max(
                0, self._stats_type_counts[entity_type_key] - 1
            )
        if category_key in self._stats_category_counts:
            self._stats_category_counts[category_key] = max(
                0, self._stats_category_counts[category_key] - 1
            )
        if point.get('is_writable', False):
            self._stats_writable_count = max(0, self._stats_writable_count - 1)

        if not self._is_suppressed():
            self.publish_enabled_state()

        log_entities.info("Disabled point %d", point_id)
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
        restarts and are only pruned when the mode itself changes.
        """
        log_restore.info("Applying mode: %s", mode_name)

        mode_value = MODES.get(mode_name)
        if mode_value is None and mode_name == "all":
            target = set(self.all_points_by_id.keys())
        else:
            target = set(mode_value) if mode_value else set()
        # Only points that actually exist in this firmware's bulk data.
        target &= set(self.all_points_by_id.keys())

        protected  = set(self.active_dynamic_points)
        to_enable  = target - self.mqtt_enabled_points
        to_disable = (self.mqtt_enabled_points - target) - protected

        # Suppress publish_enabled_state() for the duration of the enable/disable
        # loop so that each individual enable_entity/disable_entity call doesn't
        # trigger an intermediate publish. Use a single atomic check-and-increment
        # rather than _is_suppressed() + a separate lock acquisition — the two-step
        # pattern has a TOCTOU window where another thread could change the depth
        # between the read and the increment, leaving depth miscounted.
        with self._suppress_lock:
            was_suppressed = self._suppress_enabled_state_depth > 0
            if not was_suppressed:
                self._suppress_enabled_state_depth += 1

        try:
            for point_id in to_enable:
                self.enable_entity(point_id)
            for point_id in to_disable:
                self.disable_entity(point_id)
        finally:
            if not was_suppressed:
                with self._suppress_lock:
                    self._suppress_enabled_state_depth -= 1

        self.publish_enabled_state()
        self._persist_applied_mode(mode_name)
        log_restore.info(
            "Mode '%s' applied: %d enabled, %d disabled (%d total)",
            mode_name, len(to_enable), len(to_disable), len(self.mqtt_enabled_points),
        )

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

        if force:
            self.last_bulk_fetch = 0

        # Use faster polling during the post-write scan window so dynamic
        # point changes surface quickly after a switch write.
        # Outside that window use the normal user-configured poll interval.
        if self._post_write_active and current_time > self._post_write_until:
            self._post_write_active            = False
            self._post_write_controlling_point = None
            log_commands.debug("Post-write scan window ended")

        effective_interval = (
            self._post_write_interval
            if self._post_write_active
            else self.bulk_interval
        )

        if (current_time - self.last_bulk_fetch) >= effective_interval:
            failures_before = self.api_consecutive_failures
            known_count   = len(self.dynamic_point_map.all_known_dynamic_point_ids())
            should_detect = bool(known_count) or self._post_write_active
            result        = self._fetch_bulk_data(detect_changes=should_detect)
            lock_was_busy = (result is False
                             and self.api_consecutive_failures == failures_before)
            if not lock_was_busy:
                self.last_bulk_fetch = current_time

        if not self.active_entities_by_id:
            return

        log_entities.debug("Updating %d active entities", len(self.active_entities_by_id))
        with self._active_entities_lock:
            snapshot = list(self.active_entities_by_id.values())

        for entity_info in snapshot:
            self._update_entity_state(entity_info)

    def _update_entity_state(self, entity_info: dict) -> None:
        """Read the cached bulk value for one entity and publish its HA state."""
        point_id = entity_info['point_id']

        if entity_info['entity_type'] == 'button':
            self.mqtt.publish(entity_info['availability_topic'], "online", retain=True)
            return

        with self._pending_writes_lock:
            pending_entry = self.pending_writes.get(point_id)
            if pending_entry:
                age = time.time() - pending_entry.get('timestamp', 0)
                if age > _STALE_WRITE_AGE_S:
                    cmd_id = pending_entry.get('cmd_id', '?')
                    log_commands.warning(
                        "Pending write [%s] for point %d is %ds old — "
                        "the write executor may be stuck. Evicting stale entry.",
                        cmd_id, point_id, int(age),
                    )
                    self.pending_writes.pop(point_id)
                    pending_entry = None
                else:
                    # Check whether the bulk API value now matches what was
                    # written.  If so, the controller has committed — clear the
                    # pending entry so normal state publishing resumes.
                    bulk_raw = self.bulk_data.get(point_id, {}).get('raw_value')
                    if bulk_raw is not None and bulk_raw == pending_entry.get('value'):
                        log_commands.debug(
                            "Pending write [%s] for point %d confirmed by API "
                            "(raw=%s) — releasing hold",
                            pending_entry.get('cmd_id', '?'), point_id, bulk_raw,
                        )
                        self.pending_writes.pop(point_id)
                        pending_entry = None
            pending = pending_entry is not None

        if pending:
            return

        if point_id not in self.bulk_data:
            if point_id in self.mqtt_enabled_points:
                if self._post_write_active:
                    # Absence during a post-write scan means this is a dynamic
                    # point disappearing. Route through _publish_dynamic_changes
                    # so it is deindexed, its MQTT meta is cleared, the changelog
                    # is updated, and the frontend is notified — not just disabled.
                    if point_id not in (
                        self.dynamic_point_map.all_known_dynamic_point_ids()
                        - self.active_dynamic_points
                    ):
                        log_entities.info(
                            "Point %d absent during post-write scan — "
                            "treating as dynamic disappearance", point_id
                        )
                        self.baseline_point_ids.discard(point_id)
                        self._publish_dynamic_changes([], {point_id})
                else:
                    log_entities.info(
                        "Point %d absent from bulk data — disabling entity", point_id
                    )
                    self.disable_entity(point_id)
            return

        if not self.bulk_data[point_id]['is_ok']:
            self.mqtt.publish(entity_info['availability_topic'], "offline", retain=True)
            return

        data         = self.bulk_data[point_id]
        raw_value    = data['raw_value']
        string_value = data['string_value']
        metadata     = data.get('metadata', {})

        self._process_and_publish_state(entity_info, raw_value, string_value, metadata)

    def _process_and_publish_state(
        self,
        entity_info: dict,
        raw_value: int,
        string_value: str,
        metadata: dict,
        force: bool = False,
    ) -> None:
        """Process a raw value and publish the HA state for one entity."""
        point_id    = entity_info['point_id']
        entity_type = entity_info['entity_type']
        register_type = get_register_type({'metadata': metadata})

        self.mqtt.publish(entity_info['availability_topic'], "online", retain=True)

        # Sentinel value handling
        sentinel_values = {
            's16': -32768, 'u16': 65535, 's32': -2147483648, 'u32': 4294967295
        }
        variable_size = metadata.get('variableSize', '')
        if variable_size in sentinel_values and raw_value == sentinel_values[variable_size]:
            # Sentinel means "sensor not connected / no valid value".
            # Publish offline so HA shows the entity as unavailable rather
            # than a misleading zero value regardless of entity type.
            self.mqtt.publish(entity_info['availability_topic'], "offline", retain=True)
            return
        elif entity_type == 'text':
            state_value = string_value
        elif entity_type == 'switch':
            state_value = "1" if raw_value else "0"
        elif entity_type == 'binary_sensor':
            state_value = "OFF" if raw_value == 0 else "ON"
        elif entity_type == 'time':
            # Convert seconds-since-midnight to HH:MM:SS for HA time entity.
            # Firmware stores time registers as integer seconds; the
            # controller display shows HH:MM. We always emit :00 seconds.
            secs = int(raw_value) % 86400
            state_value = f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}:00"
        elif entity_type == 'sensor' and point_id == 2685:
            # Next periodic increase date — stored as days since 2010-01-01.
            # Convert to ISO date string for HA device_class=date sensor.
            try:
                from datetime import date, timedelta
                d = date(2010, 1, 1) + timedelta(days=int(raw_value))
                state_value = d.isoformat()  # YYYY-MM-DD
            except (ValueError, OverflowError):
                state_value = str(raw_value)
        elif entity_type == 'sensor' and point_id in (2453, 14987):
            # EB101 firmware version — encoded as major<<12 | minor<<6 | patch.
            # e.g. 12481 → 3.3.1. Confirmed from S2125-12 firmware 3.3.1.
            v = int(raw_value)
            major = (v >> 12) & 0x3F
            minor = (v >> 6)  & 0x3F
            patch = v         & 0x3F
            state_value = f"{major}.{minor}.{patch}"
        elif entity_type == 'sensor' and point_id == 2509:
            # SMO S40 (EB100) firmware version — encoded as major<<8 | minor.
            # e.g. 1035 = 0x040B → 4.11 (patch not available in this register).
            v = int(raw_value)
            major = (v >> 8) & 0xFF
            minor = v & 0xFF
            state_value = f"{major}.{minor}"
        elif entity_type == 'sensor' and point_id == 2022:
            # Current status bitfield — community-decoded from SMO S40 register 31121.
            # Mode bits (high): 14=HW boost, 13=Hot water, 12=Heating, 20=Cooling
            # State bits (low): 4=Compressor starting, 2=Compressor running, 3=Pump running
            v = int(raw_value)
            _MODE_BITS = {
                20: "Cooling",
                14: "Hot water boost",
                13: "Hot water",
                12: "Heating",
            }
            # Compressor state: bit4=starting, bit2+4=running, neither=preheating/pump only
            modes = [label for bit, label in _MODE_BITS.items() if v & (1 << bit)]
            v_bit2 = bool(v & (1 << 2))
            v_bit4 = bool(v & (1 << 4))
            if v_bit2 and v_bit4:
                comp_state = "Running"
            elif v_bit4:
                comp_state = "Starting"
            elif modes:  # mode active but no compressor bits
                comp_state = "Preheating"
            else:
                comp_state = ""
            if modes:
                mode_str = ' + '.join(modes)
                state_value = f"{mode_str} ({comp_state})" if comp_state else mode_str
            else:
                state_value = 'Idle'
        elif entity_type == 'select':
            mapping = entity_info.get('value_mapping')
            if mapping is None:
                mapping = get_value_mapping(
                    point_id, entity_info.get('point_data', {}), register_type
                )
                if mapping is not None:
                    # Cache miss — populate for future polls
                    entity_info['value_mapping'] = mapping
            if mapping and raw_value in mapping:
                state_value = mapping[raw_value]
            else:
                state_value = str(raw_value)
        elif entity_type == 'sensor':
            mapping = entity_info.get('value_mapping')
            if mapping is None:
                mapping = get_value_mapping(
                    point_id, entity_info.get('point_data', {}), register_type
                )
                if mapping is not None:
                    # Cache miss — populate for future polls
                    entity_info['value_mapping'] = mapping
            if mapping and raw_value in mapping:
                state_value = mapping[raw_value]
            else:
                divisor     = metadata.get('divisor', 1)
                state_value = apply_divisor(raw_value, divisor)
        else:
            divisor     = metadata.get('divisor', 1)
            state_value = apply_divisor(raw_value, divisor)

        change_threshold = metadata.get('change', 0)
        should_pub = force or self.value_cache.should_publish(
            point_id, raw_value, change_threshold, min_interval=self.bulk_interval
        )

        if should_pub or point_id not in self.last_states or self.last_states[point_id] != state_value:
            if not entity_info.get('state_topic'):
                log_entities.warning(
                    "Point %d (%s): no state_topic — cannot publish state",
                    point_id, entity_info.get('entity_type'),
                )
                return
            self.mqtt.publish(entity_info['state_topic'], state_value, retain=True)
            self.last_states[point_id] = state_value


    # ------------------------------------------------------------------ #
    # Bulk data fetch                                                      #
    # ------------------------------------------------------------------ #

    def _fetch_bulk_data(
        self,
        detect_changes: bool = True,
    ) -> bool | int:
        """Fetch bulk data from the API and process all points.

        Protected by _bulk_fetch_lock so concurrent callers (e.g. a force
        poll and the normal polling loop) never run simultaneously.

        Returns:
          int > 0  — number of dynamic changes detected
          True     — success, no changes
          False    — failure or lock busy
        """
        if not self._bulk_fetch_lock.acquire(blocking=False):
            log_discovery.info(
                "Bulk fetch skipped — a previous fetch is still running "
                "(API may be slow; consider raising poll_interval)"
            )
            return False
        try:
            start_time = time.time()
            response   = self._api.fetch_bulk_points()

            if not response or not isinstance(response, dict):
                log_discovery.warning(
                    "Bulk fetch returned an unexpected response type (%s) — "
                    "expected a JSON object. API may be temporarily unavailable.",
                    type(response).__name__,
                )
                self._handle_api_failure()
                return False

            current_point_ids = set()
            new_points        = []
            if self._post_write_active:
                scan_type = "post-write-scan"
            elif detect_changes:
                known_count = len(self.dynamic_point_map.all_known_dynamic_point_ids())
                scan_type = f"dynamic-monitoring({known_count} known)"
            else:
                scan_type = "poll"
            log_discovery.debug(
                "Processing bulk response: %d points (%s)",
                len(response), scan_type,
            )

            now = time.time()

            for point_id_str, point_data in response.items():
                try:
                    point_id = int(point_id_str)
                    current_point_ids.add(point_id)

                    value_data = point_data.get('value', {})
                    metadata   = point_data.get('metadata', {})

                    # ── String cache (Finding 2) ──────────────────────────────
                    # title and description never change between firmware updates.
                    # Cache the clean_string result; only recompute when the raw
                    # string from the API actually differs.
                    raw_title = point_data.get('title', f'Point {point_id}')
                    raw_desc  = point_data.get('description', '')
                    cached_strings = self._point_string_cache.get(point_id)
                    if (cached_strings is None
                            or cached_strings[0] != raw_title
                            or cached_strings[1] != raw_desc):
                        title       = clean_string(raw_title)
                        description = clean_string(raw_desc)
                        self._point_string_cache.put(point_id, (
                            raw_title, raw_desc, title, description
                        ))
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
                            'raw_value':    value_data.get('integerValue', 0),
                            'string_value': value_data.get('stringValue', ''),
                            'is_ok':        value_data.get('isOk', False),
                            'metadata':     metadata,
                            'title':        title,
                            'description':  description,
                            'timestamp':    now,
                        }
                    else:
                        existing['raw_value']    = value_data.get('integerValue', 0)
                        existing['string_value'] = value_data.get('stringValue', '')
                        existing['is_ok']        = value_data.get('isOk', False)
                        existing['timestamp']    = now
                        # Metadata and strings change only on firmware update —
                        # update in place to avoid unnecessary allocation.
                        if existing.get('title') != title:
                            existing['title']    = title
                        if existing.get('description') != description:
                            existing['description'] = description
                        if existing.get('metadata') != metadata:
                            existing['metadata'] = metadata

                    if (detect_changes
                            and self.initial_discovery_complete
                            and point_id not in self.baseline_point_ids
                            and point_id not in self.published_configs):

                        if self._post_write_active:
                            # Point appeared during post-write scan window.
                            # Route through _publish_dynamic_changes regardless
                            # of whether it is a known dynamic point or newly
                            # discovered — both are treated as dynamic appearances.
                            new_points.append((point_id, point_data))

                        else:
                            # Point appeared outside any scan window.
                            # Check whether it is a known dynamic point first —
                            # this covers the case where a bulk fetch caught a
                            # dynamic point appearance after the scan window expired.
                            # Routing a known dynamic point as static would
                            # permanently misclassify it.
                            if self.dynamic_point_map.is_known_dynamic(point_id):
                                log_discovery.info(
                                    "Known dynamic point %d '%s' appeared outside "
                                    "scan window — routing as dynamic",
                                    point_id, title,
                                )
                                new_points.append((point_id, point_data))
                            else:
                                # Genuinely unknown point — permanent firmware
                                # addition (e.g. firmware update).
                                log_discovery.info(
                                    "New permanent point %d '%s' appeared during "
                                    "normal poll — indexing as static",
                                    point_id, title,
                                )
                                entity_type_p, category_p = self._get_cached_entity_type({
                                    'variableId':  point_id,
                                    'metadata':    metadata,
                                    'title':       title,
                                    'description': description,
                                })
                                self._index_point({
                                    'variableId':      point_id,
                                    'display_title':   title,
                                    'description':     description,
                                    'metadata':        metadata,
                                    'entity_type':     entity_type_p,
                                    'entity_category': category_p,
                                    'is_writable':     metadata.get('isWritable', False),
                                    'is_dynamic':      False,
                                })
                                self.baseline_point_ids.add(point_id)
                                if entity_type_p in ('switch', 'select'):
                                    self.dynamic_point_map.populate_from_bulk(
                                        {point_id: self.all_points_by_id.get(point_id, {})},
                                        {point_id: entity_type_p},
                                    )
                                point_obj = self.all_points_by_id.get(point_id)
                                if point_obj:
                                    self._pub.publish_point_metadata(point_obj)
                                self._pub.publish_point_list(self.all_points_by_id)

                    if not detect_changes:
                        if not self.dynamic_point_map.is_known_dynamic(point_id):
                            entity_type, category = self._get_cached_entity_type({
                                'variableId': point_id,
                                'metadata':   metadata,
                                'title':      title,
                                'description': description,
                            })
                            self._index_point({
                                'variableId':     point_id,
                                'display_title':  title,
                                'description':    description,
                                'metadata':       metadata,
                                'entity_type':    entity_type,
                                'entity_category': category,
                                'is_writable':    metadata.get('isWritable', False),
                                'is_dynamic':     False,
                            })

                except (ValueError, KeyError) as e:
                    log_discovery.warning("Error processing point %s: %s", point_id_str, e)
                    continue

            # ── Remove points that disappeared from the API response ───────────
            # (Finding 4: maintain bulk_data in-place rather than full rebuild)
            for gone_id in set(self.bulk_data.keys()) - current_point_ids:
                self.bulk_data.pop(gone_id, None)
                self._point_string_cache.pop(gone_id, None)

            disappeared_points = set()
            if detect_changes and self.initial_discovery_complete:
                known_dynamic_ids = self.dynamic_point_map.all_known_dynamic_point_ids()

                disappeared_points = {
                    pid for pid in known_dynamic_ids & self.active_dynamic_points
                    if pid not in current_point_ids
                }

                # During post-write scan: baseline points that went absent
                # are newly discovered dynamic disappearances.
                if self._post_write_active:
                    newly_absent = (
                        self.baseline_point_ids
                        - current_point_ids
                        - known_dynamic_ids
                    )
                    for pid in newly_absent:
                        log_discovery.debug(
                            "Point %d absent during post-write scan — "
                            "treating as dynamic disappearance", pid,
                        )
                        self.baseline_point_ids.discard(pid)
                        disappeared_points.add(pid)

                if disappeared_points:
                    log_discovery.debug(
                        "Dynamic points absent from this fetch: %s",
                        sorted(disappeared_points),
                    )

            self.published_configs = current_point_ids

            if detect_changes and (new_points or disappeared_points):
                log_discovery.debug(
                    "Dynamic changes detected: +%d new, -%d disappeared",
                    len(new_points), len(disappeared_points),
                )
                self._publish_dynamic_changes(new_points, disappeared_points)
            elif detect_changes and self._post_write_active:
                log_discovery.debug(
                    "Post-write scan: no dynamic changes yet (known=%d)",
                    len(self.dynamic_point_map.all_known_dynamic_point_ids()),
                )

            elapsed = time.time() - start_time
            self.last_fetch_duration = elapsed

            if self.api_consecutive_failures >= self.api_failure_threshold:
                if self._api_notification_active and self.mqtt:
                    self._dismiss(self.mqtt, _NOTIF_API_UNREACHABLE)
                    self._api_notification_active = False
                    # Publish a structured resolution alert so automations know
                    # the API outage has cleared — mirrors the "api_unreachable"
                    # alert published in _handle_api_failure.
                    if self._pub:
                        self._pub.publish_bridge_alert(
                            alert_type = "api_restored",
                            severity   = "info",
                            message    = (
                                f"Nibe API contact restored after "
                                f"{self.api_consecutive_failures} failed polls."
                            ),
                            context    = {
                                "previous_failure_count": self.api_consecutive_failures,
                                "api_url":                self._api.base_url,
                            },
                        )

            if self._discovery_notification_active and self.mqtt:
                self._dismiss(self.mqtt, _NOTIF_DISCOVERY_INCOMPLETE)
                self._discovery_notification_active = False

            self.api_consecutive_failures = 0
            self.api_last_success_time    = time.time()

            log_discovery.debug("Processed %d points in %.2fs", len(current_point_ids), elapsed)
            return bool(new_points or disappeared_points) if detect_changes else True

        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                log_discovery.warning(
                    "Bulk fetch rejected (HTTP %d) — credentials may not yet be "
                    "configured on the controller. Will retry.",
                    e.code,
                )
            else:
                log_discovery.warning(
                    "Bulk fetch failed with HTTP %d — will retry.", e.code,
                )
            self._handle_api_failure()
            return False
        except Exception as e:
            log_discovery.error(
                "Unhandled exception during bulk fetch — this is a bug, "
                "please report it: %s", e, exc_info=True,
            )
            self._handle_api_failure()
            return False
        finally:
            self._bulk_fetch_lock.release()

    def _handle_api_failure(self) -> None:
        self.api_consecutive_failures += 1
        if (self.api_consecutive_failures >= self.api_failure_threshold
                and not self._api_notification_active
                and self.mqtt):
            model = self.device_info.get('model', 'S-series')
            msg   = (
                f"The Nibe {model} REST API has not responded for "
                f"{self.api_consecutive_failures} consecutive polls "
                f"({self.api_consecutive_failures * self.bulk_interval}s). "
                f"Check that the controller is reachable at {self._api.base_url}. "
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
                    alert_type = "api_unreachable",
                    severity   = "error",
                    message    = msg,
                    context    = {
                        "consecutive_failures": self.api_consecutive_failures,
                        "failure_threshold":    self.api_failure_threshold,
                        "api_url":              self._api.base_url,
                    },
                )
            self._api_notification_active = True

    # ------------------------------------------------------------------ #
    # Dynamic point changes                                                #
    # ------------------------------------------------------------------ #

    def _publish_dynamic_changes(
        self,
        new_points:          list[tuple[int, dict]],
        disappeared_points:  set[int],
    ) -> None:
        if not new_points and not disappeared_points:
            return

        log_discovery.info(
            "Publishing dynamic changes: +%d -%d", len(new_points), len(disappeared_points)
        )

        change_event: dict[str, object] = {
            'timestamp':     time.time(),
            'iso_timestamp': _fmt_ts(),
            'added':         [],
            'removed':       [],
            'source':        'firmware',
            'triggered_by':  None,
        }

        with self._suppress_enabled_state():
            for point_id, point_data in new_points:
                metadata    = point_data.get('metadata', {})
                title       = clean_string(point_data.get('title', f'Point {point_id}'))
                description = clean_string(point_data.get('description', ''))

                entity_type, category = self._get_cached_entity_type({
                    'variableId': point_id,
                    'metadata':   metadata,
                    'title':      title,
                    'description': description,
                })
                processed = {
                    'variableId':      point_id,
                    'display_title':   title,
                    'description':     description,
                    'metadata':        metadata,
                    'entity_type':     entity_type,
                    'entity_category': category,
                    'is_writable':     metadata.get('isWritable', False),
                    'is_dynamic':      True,
                }
                self._index_point(processed)
                self._pub.publish_point_metadata(processed)
                self.enable_entity(point_id)
                self.active_dynamic_points.add(point_id)
                
                change_event['added'].append({  # type: ignore[attr-defined]
                    'id': point_id, 'title': title,
                    'type': entity_type, 'is_dynamic': True,
                })
                log_discovery.info(
                    "Dynamic entity appeared: %d - %s (%s)", point_id, title, entity_type
                )

        # Record all new points in dynamic_point_map via record_outcome
        # so the map is populated identically regardless of learning mode.
        # This ensures known_dynamic is correct for dashboard suppression
        # and injection — both in learning mode and normal operation.
        controlling = self._post_write_controlling_point
        if controlling and new_points:
            new_pids = [p for p, _ in new_points]
            controlling_raw = self.bulk_data.get(controlling, {}).get('raw_value', 1)
            entry = self.dynamic_point_map.get(controlling)
            if entry is None:
                # First time this controlling point is seen — create the entry
                controlling_point = self.all_points_by_id.get(controlling, {})
                controlling_meta  = controlling_point.get('metadata', {})
                mn = controlling_meta.get('minValue', 0)
                mx = controlling_meta.get('maxValue', 1)
                entry = DynamicPointEntry(
                    point_id          = controlling,
                    title             = controlling_point.get('display_title')
                                        or controlling_point.get('title', f'Point {controlling}'),
                    entity_type       = controlling_point.get('entity_type', 'switch'),
                    unprocessed_values = set(range(int(mn), int(mx) + 1)),
                )
                self.dynamic_point_map._table[controlling] = entry
                log_discovery.debug(
                    "Created dynamic map entry for controlling point %d", controlling
                )
            self.dynamic_point_map.record_outcome(controlling, controlling_raw, new_pids)
            self._persist_dynamic_map()
            log_discovery.debug(
                "Recorded %d dynamic point(s) under controlling point %d "
                "(value=%d) via post-write scan",
                len(new_pids), controlling, controlling_raw,
            )

        for point_id in disappeared_points:
            entity = self.all_points_by_id.get(point_id)
            if entity:
                self._deindex_point(point_id)
                self._pub.invalidate_config_hash(point_id)
                self.mqtt.publish(BrowserTopic.META_TEMPLATE.format(id=point_id), "", retain=True)
                if point_id in self.mqtt_enabled_points:
                    self.disable_entity(point_id)
                self.active_dynamic_points.discard(point_id)
                change_event['removed'].append({  # type: ignore[attr-defined]
                    'id':         point_id,
                    'title':      entity.get('display_title', f'Point {point_id}'),
                    'type':       entity.get('entity_type', 'unknown'),
                    'is_dynamic': True,
                })
                log_discovery.info("Dynamic entity disappeared: %d", point_id)
                # Persist active set immediately after removal — write-ahead
                # ordering so a crash after this line leaves correct state.
                self._persist_active_dynamic()

        if new_points:
            self.publish_enabled_state()

        if disappeared_points:
            self.publish_enabled_state()

        if not change_event['added'] and not change_event['removed']:  # type: ignore[attr-defined]
            return

        # Persist active_dynamic_points for appearance events.
        # Disappearances are already persisted per-point above for crash safety.
        if new_points:
            self._persist_active_dynamic()

        self._pub.publish_point_list(self.all_points_by_id)

        # Populate triggered_by now that added/removed are fully built.
        # _post_write_controlling_point is set when a write activates the scan
        # window; None for startup / periodic-poll discoveries.
        controlling = self._post_write_controlling_point
        if controlling:
            cp = self.all_points_by_id.get(controlling, {})
            ctrl_title = cp.get('display_title') or cp.get('title', f'Point {controlling}')
            ctrl_value = self.bulk_data.get(controlling, {}).get('raw_value')
            change_event['triggered_by'] = {
                'id':    controlling,
                'title': ctrl_title,
                **(({'value': ctrl_value}) if ctrl_value is not None else {}),
            }

        self._update_changelog_history(change_event)
        self.mqtt.publish(
            BrowserTopic.DYNAMIC, json.dumps(change_event), retain=False
        )

        # Send a persistent HA notification so the user knows the dashboard
        # was updated and needs a browser reload to show the changes.
        try:
            added:   list[dict] = change_event['added']    # type: ignore[assignment]
            removed: list[dict] = change_event['removed']  # type: ignore[assignment]
            trig: dict | None   = change_event.get('triggered_by')  # type: ignore[assignment]
            ctrl_title = trig['title'] if trig else ''
            ctrl_menu  = ''
            if trig:
                menu_entry = self.point_to_menu_map.get(trig['id'])
                ctrl_menu  = f"menu {menu_entry[0]} — {menu_entry[1]}" if menu_entry else ''
            log_discovery.debug(
                "Dynamic change notification: controlling=%s added=%d removed=%d",
                trig['id'] if trig else None, len(added), len(removed),
            )

            if added:
                point_lines = '\n'.join(
                    f"- **{p['title']}** (point {p['id']})" for p in added
                )
                ctrl_line = f"\nTriggered by: **{ctrl_title}**" if ctrl_title else ''
                menu_line = f" in {ctrl_menu}" if ctrl_menu else ''
                message = (
                    f"The Nibe Menus dashboard was updated — "
                    f"{len(added)} new setting(s) are now available{menu_line}:"
                    f"{ctrl_line}\n\n"
                    f"{point_lines}\n\n"
                    f"[Open Nibe Menus dashboard](/nibe-menus) and reload the page "
                    f"to see the new settings."
                )
                self._notify(
                    self.mqtt,
                    title           = "Nibe Menus — Dashboard updated",
                    message         = message,
                    notification_id = "nibe_dashboard_updated",
                )

            if removed:
                point_lines = '\n'.join(
                    f"- **{p['title']}** (point {p['id']})" for p in removed
                )
                ctrl_line = f"\nTriggered by: **{ctrl_title}**" if ctrl_title else ''
                menu_line = f" in {ctrl_menu}" if ctrl_menu else ''
                message = (
                    f"The Nibe Menus dashboard was updated — "
                    f"{len(removed)} setting(s) are no longer available{menu_line}:"
                    f"{ctrl_line}\n\n"
                    f"{point_lines}\n\n"
                    f"[Open Nibe Menus dashboard](/nibe-menus) and reload the page."
                )
                self._notify(
                    self.mqtt,
                    title           = "Nibe Menus — Dashboard updated",
                    message         = message,
                    notification_id = "nibe_dashboard_updated",
                )
        except (ValueError, TypeError, AttributeError) as e:
            log_discovery.debug("Could not send dashboard update notification: %s", e)
        except Exception as e:
            log_discovery.error("Unexpected error sending dashboard notification: %s", e, exc_info=True)

    # ------------------------------------------------------------------ #
    # Write commands                                                       #
    # ------------------------------------------------------------------ #


    def _parse_command_payload(
        self,
        payload:     str,
        entity_info: dict,
        cmd_id:      str,
    ) -> int | float | str | None:
        """Parse and validate a raw MQTT payload string for the given entity type.

        Returns the converted integer/float/string value to write to the API,
        or ``None`` if the payload is invalid and the command should be dropped.

        Separated from ``_handle_command`` so the conversion logic can be unit-
        tested independently of MQTT callback plumbing.
        """
        point_id      = entity_info['point_id']
        entity_type   = entity_info['entity_type']
        register_type = get_register_type({'metadata': entity_info['metadata']})

        if entity_type == 'button':
            return 1

        if entity_type in ('switch', 'binary_sensor'):
            return 1 if payload in ("1", "ON", "on", "true", "True") else 0

        if entity_type == 'time':
            # Convert HH:MM:SS (or HH:MM) from HA time entity to seconds.
            try:
                parts = payload.strip().split(':')
                h, m = int(parts[0]), int(parts[1])
                return h * 3600 + m * 60
            except (ValueError, IndexError):
                log_commands.warning(
                    "[%s] Invalid time value: '%s' — expected HH:MM:SS", cmd_id, payload
                )
                return None

        if entity_type == 'select':
            mapping = get_value_mapping(
                point_id, entity_info.get('point_data', {}), register_type
            )
            if mapping:
                reverse_map = {v.strip(): k for k, v in mapping.items()}
                if payload in reverse_map:
                    return reverse_map[payload]
                log_commands.warning("[%s] Invalid select option: '%s'", cmd_id, payload)
                return None
            try:
                return int(payload)
            except ValueError:
                log_commands.warning("[%s] Invalid numeric value: '%s'", cmd_id, payload)
                return None

        if entity_type == 'number':
            divisor = entity_info['metadata'].get('divisor', 1) or 1
            try:
                value = reverse_divisor(float(payload), divisor)
            except ValueError:
                log_commands.warning("[%s] Invalid number: '%s'", cmd_id, payload)
                return None
            if not entity_info.get('is_degenerate_range', False):
                metadata = entity_info.get('metadata', {})
                min_val  = metadata.get('minValue')
                max_val  = metadata.get('maxValue')
                if min_val is not None and value < min_val:
                    log_commands.warning(
                        "[%s] Number out of range: %s < min %s for point %d",
                        cmd_id, value, min_val, point_id,
                    )
                    if point_id in self.last_states:
                        self.mqtt.publish(
                            entity_info['state_topic'],
                            self.last_states[point_id], retain=True,
                        )
                    return None
                if max_val is not None and value > max_val:
                    log_commands.warning(
                        "[%s] Number out of range: %s > max %s for point %d",
                        cmd_id, value, max_val, point_id,
                    )
                    if point_id in self.last_states:
                        self.mqtt.publish(
                            entity_info['state_topic'],
                            self.last_states[point_id], retain=True,
                        )
                    return None
            return value

        if entity_type == 'text':
            sanitised = ''.join(c for c in payload if c.isprintable())
            if len(sanitised) > _TEXT_REGISTER_MAX_LEN:
                log_commands.warning(
                    "[%s] Text payload for point %d truncated from %d to %d chars",
                    cmd_id, point_id, len(sanitised), _TEXT_REGISTER_MAX_LEN,
                )
                sanitised = sanitised[:_TEXT_REGISTER_MAX_LEN]
            if sanitised != payload:
                log_commands.debug(
                    "[%s] Text payload for point %d sanitised", cmd_id, point_id,
                )
            return sanitised

        return None  # unknown entity type — no write path

    def _run_learning_detection(
        self,
        point_id:   int,
        value:      int,
        cmd_id:     str,
    ) -> None:
        """Wait for a clean bulk fetch cycle and record the outcome in the map.

        Called in the write executor (serialised) after a successful write to
        an unprocessed switch/select in learning mode.

        The detection window runs for the full _POST_WRITE_SCAN_S (90s) —
        the firmware has an internal ~1 minute cycle before dynamic points
        appear in the bulk fetch.  We must not terminate early on a quiet
        poll; we wait the full window and record whatever appeared.

        If new points appear partway through the window, we extend by one
        more full quiet period to catch any stragglers.

        Parameters
        ----------
        point_id :
            The controlling switch/select that was written.
        value :
            The integer value that was written.
        cmd_id :
            Correlation token for log tracing.
        """
        prefix = f"[{cmd_id}] "
        log_commands.info(
            "%sLearning detection started for point %d value=%d "
            "(waiting up to %ds for firmware cache refresh)",
            prefix, point_id, value, _POST_WRITE_SCAN_S,
        )

        # Snapshot point set before detection
        points_before = set(self.bulk_data.keys())

        # Activate post-write scan mode to get 5s polling
        self._post_write_active = True
        self._post_write_until  = time.time() + _POST_WRITE_SCAN_S

        poll_interval    = self._post_write_interval   # 5s
        deadline         = time.time() + _POST_WRITE_SCAN_S
        last_size        = len(points_before)

        while True:
            time.sleep(poll_interval)
            current_size = len(self.bulk_data)

            if current_size != last_size:
                # Change detected — controller cache refreshed. Stop immediately;
                # there is no value in waiting longer since the bulk fetch has
                # already shown us the full post-write state.
                log_commands.debug(
                    "%sLearning: bulk size changed (%d→%d) — detection complete",
                    prefix, last_size, current_size,
                )
                break

            if time.time() >= deadline:
                # Full window elapsed with no changes — no dynamic points.
                break

        # Calculate what appeared
        points_after  = set(self.bulk_data.keys())
        new_point_ids = sorted(points_after - points_before)

        # Record outcome in map
        self.dynamic_point_map.record_outcome(point_id, value, new_point_ids)
        self._persist_dynamic_map()

        log_commands.info(
            "%sLearning detection complete for point %d value=%d: "
            "%d new point(s) %s",
            prefix, point_id, value,
            len(new_point_ids),
            new_point_ids if new_point_ids else "(none)",
        )

    def _handle_command(self, entity_info: dict, message) -> None:
        """Decode the MQTT payload and dispatch a write to the worker thread.

        Payload parsing is delegated to ``_parse_command_payload`` so this method
        stays focused on MQTT decode, correlation ID generation, pending-write
        registration, and executor submission.
        """
        try:
            payload = message.payload.decode('utf-8').strip()
        except UnicodeDecodeError:
            log_commands.warning(
                "Malformed UTF-8 payload on topic %s — ignoring", message.topic
            )
            return
        
        point_id = entity_info['point_id']
        cmd_id   = uuid.uuid4().hex[:_CMD_ID_LENGTH]

        log_commands.info(
            "[%s] Command received for %s %d: '%s'",
            cmd_id, entity_info['entity_type'], point_id, payload,
        )

        value = self._parse_command_payload(payload, entity_info, cmd_id)
        if value is None:
            return

        with self._pending_writes_lock:
            self.pending_writes[point_id] = {
                'point_id':  point_id,
                'value':     value,
                'payload':   payload,
                'timestamp': time.time(),
                'cmd_id':    cmd_id,
            }

        self._write_executor.submit(
            self._handle_command_worker, entity_info, value, payload, cmd_id
        )

    def _handle_command_worker(
        self,
        entity_info: dict,
        value: int | float | str,
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
        point_id    = entity_info['point_id']
        entity_type = entity_info['entity_type']
        prefix      = f"[{cmd_id}] " if cmd_id else ""

        log_commands.info("%sWriting %s to point %d", prefix, value, point_id)

        self._write_total += 1
        success = self._api.write_point(point_id, value, entity_info)

        if success:
            self._write_success += 1
            log_commands.info("%sWrite successful for point %d", prefix, point_id)
            if self._write_notification_active and self.mqtt:
                self._dismiss(self.mqtt, _NOTIF_WRITE_ERROR)
                self._write_notification_active = False
                if self._pub:
                    self._pub.publish_bridge_alert(
                        alert_type = "write_restored",
                        severity   = "info",
                        message    = f"Write to point {point_id} succeeded — previous error cleared.",
                        context    = {"point_id": point_id, "cmd_id": cmd_id},
                    )

            # Publish optimistic state immediately so HA UI snaps to the
            # new value without waiting for the next bulk fetch cycle.
            # The pending write entry suppresses any stale bulk-fetch
            # republish until the firmware confirms the new value.
            if entity_type == 'switch':
                state_value = "1" if value else "0"
            elif entity_type == 'time':
                secs = int(value) % 86400
                state_value = f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}:00"
            elif entity_type in ('select', 'number'):
                state_value = payload
            else:
                state_value = str(value)
            if entity_info.get('state_topic'):
                self.mqtt.publish(entity_info['state_topic'], state_value, retain=True)
                self.last_states[point_id] = state_value

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
            if entity_type in ('switch', 'select'):
                int_value = int(value) if isinstance(value, (int, float)) else None
                entry     = self.dynamic_point_map.get(point_id)

                if entry is not None and entry.is_fully_processed() and not entry.is_controlling:
                    # Case A1: non-controlling — no scan needed.
                    log_commands.debug(
                        "%sPoint %d is non-controlling — no dynamic scan needed",
                        prefix, point_id,
                    )

                elif (entry is not None
                        and entry.is_fully_processed()
                        and entry.is_controlling
                        and not entry.firmware_removed):
                    # Case A2: fully-processed controlling — open the post-write
                    # scan window. The bulk fetch will detect dynamic point
                    # appearances/disappearances within the 90s window.
                    # (Fast-path single-point probing was removed: hardware testing
                    # on the S2125 showed the firmware takes >12.5s to activate
                    # a dynamic point after a REST write, so all probes missed and
                    # the post-write scan caught it anyway.)
                    log_commands.debug(
                        "%sPoint %d is fully-processed controlling — opening scan window",
                        prefix, point_id,
                    )
                    self._post_write_controlling_point = point_id
                    self._post_write_active            = True
                    self._post_write_until             = time.time() + self._post_write_duration

                else:
                    # Case A3 / B: unprocessed value or not in map.
                    # DynamicPointMap learning is always active — outcomes are
                    # always recorded so the map self-populates without manual
                    # intervention. The detection window runs and any dynamic
                    # changes observed are attributed to this write.
                    self._post_write_active            = True
                    self._post_write_until             = time.time() + self._post_write_duration
                    self._post_write_controlling_point = point_id
                    if entry is not None and int_value is not None and int_value in entry.unprocessed_values:
                        log_commands.info(
                            "%sPoint %d value=%d is unprocessed — "
                            "starting detection window (learning always active)",
                            prefix, point_id, int_value,
                        )
                        self._run_learning_detection(point_id, int_value, cmd_id)
                    else:
                        log_commands.debug(
                            "%sPost-write scan activated (%ds) for point %d (%s)",
                            prefix, self._post_write_duration, point_id,
                            "unprocessed" if entry is not None else "not in map",
                        )

        else:
            self._write_failed += 1
            self._last_write_error = (
                f"point {point_id} value '{payload}' at {_fmt_ts()}"
            )
            log_commands.error("%sWrite failed for point %d", prefix, point_id)
            # Pop pending_writes on failure so the point isn't blocked
            # from normal state updates by a stale pending entry.
            with self._pending_writes_lock:
                self.pending_writes.pop(point_id, None)
            point_title = entity_info.get('display_title', f'point {point_id}')
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
                        alert_type = "write_failed",
                        severity   = "error",
                        message    = msg,
                        context    = {
                            "point_id":    point_id,
                            "point_title": point_title,
                            "value":       payload,
                            "cmd_id":      cmd_id,
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
        point_id = entity_info['point_id']
        response = self._api.fetch_point(point_id)
        if not response:
            log_commands.debug(
                "Force readback for point %d skipped — point absent "
                "(dynamic point inactive or network error)", point_id,
            )
            return
        dv = response.get('value', {})
        if not isinstance(dv, dict) or not dv.get('isOk'):
            log_commands.warning("Force readback for point %d: value not OK", point_id)
            return
        self._process_and_publish_state(
            entity_info,
            dv.get('integerValue', 0),
            dv.get('stringValue', ''),
            response.get('metadata', {}),
            force=True,
        )

    # ------------------------------------------------------------------ #
    # MQTT reconnect helpers                                               #
    # ------------------------------------------------------------------ #

    def register_mgmt_subscription(self, topic: str, handler, qos: int = 1) -> None:
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

        The value cache is cleared on reconnect so that all entity states are
        republished immediately rather than waiting for a value change — HA
        loses all retained state when the broker restarts.
        """
        self.value_cache = ValueCache()
        self.last_bulk_fetch = 0
        entity_count = 0
        with self._active_entities_lock:
            snapshot = list(self.active_entities_by_id.values())
        for entity_info in snapshot:
            topic = entity_info.get('command_topic')
            if not topic:
                continue
            def _make_handler(ei):
                def handler(_client, _userdata, message):
                    self._handle_command(ei, message)
                return handler
            self.mqtt.subscribe(topic, qos=1)
            self.mqtt.message_callback_add(topic, _make_handler(entity_info))
            entity_count += 1

        mgmt_count = 0
        for topic, handler, qos in self._mgmt_subscriptions:
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

        log_mqtt.info(
            "Reconnect: re-subscribed to %d entity command topic(s), "
            "%d management topic(s), 2 changelog topic(s), and dynamic map",
            entity_count, mgmt_count,
        )

    def republish_availability(self) -> None:
        """Republish 'online' for all active entities after a broker restart."""
        with self._active_entities_lock:
            snapshot = list(self.active_entities_by_id.values())
        if not snapshot:
            return
        for entity_info in snapshot:
            self.mqtt.publish(entity_info['availability_topic'], "online", retain=True)
        if self._mgmt_avail_topic:
            self.mqtt.publish(self._mgmt_avail_topic, "online", retain=True)
        log_mqtt.info("Reconnect: republished availability for %d entities", len(snapshot))

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
            except Exception as e:
                log_entities.debug("on_enabled_state_change callback error: %s", e)
        else:
            self._last_published_enabled = current

    def set_on_enabled_state_change(self, callback) -> None:
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
        if '.' not in ha_entity_id:
            return None
        domain, slug = ha_entity_id.split('.', 1)

        if slug.startswith('nibe_'):
            try:
                return int(slug[5:])
            except ValueError:
                pass

        candidate = f'homeassistant/{domain}/{slug}/config'
        with self._active_entities_lock:
            for pid, ei in self.active_entities_by_id.items():
                known = f'homeassistant/{ei["entity_type"]}/{ei["entity_id"]}/config'
                if known == candidate:
                    return pid

        if unique_id_map:
            for uid, eid in unique_id_map.items():
                if eid == ha_entity_id and uid.startswith('nibe_'):
                    try:
                        return int(uid[5:])
                    except ValueError:
                        pass

        return None

    def build_disable_notification(
        self,
        point_id: int | None,
        ha_entity_id: str,
        action: str,
    ) -> tuple[str, str, str]:
        """Build (title, message, notification_id) for a HA-side entity disable/enable event."""
        point      = self.all_points_by_id.get(point_id) if point_id else None
        is_dynamic = point.get('is_dynamic', False) if point else False

        if point:
            display = f'#{point_id} ({point.get("display_title", f"Point {point_id}")})'
        elif point_id:
            display = f'#{point_id}'
        else:
            display = ha_entity_id

        safe_id  = ha_entity_id.replace('.', '_').replace('-', '_')[:60]
        notif_id = f'nibe_ha_disable_{safe_id}'

        if action == 're-enabled':
            return (
                'Nibe Bridge: Entity re-enabled in HA',
                (f'Data point {display} was re-enabled via the HA entity settings. '
                 f'The bridge will resume publishing its state on the next poll cycle.'),
                notif_id,
            )

        if is_dynamic:
            # Look up the controlling switch from the persistent controller map.
            # The controller map maps ctrl_id → {dynamic_points: [...]}, so we
            # reverse-search for the controlling point of this dynamic point.
            context = (
                'This entity appeared during a firmware-controlled state change. '
                'It will disappear automatically when the operating mode that '
                'activates it is no longer active.'
            )
            return (
                'Nibe Bridge: Dynamic entity disabled in HA',
                (f'Dynamic data point {display} was disabled via the HA entity settings. '
                 f'The bridge has kept the entity enabled — it is still being polled.\n\n'
                 f'Please go to Settings > Entities and re-enable it.\n\n{context}'),
                notif_id,
            )

        return (
            'Nibe Bridge: Entity disabled in HA',
            (f'Data point {display} was disabled via the HA entity settings. '
             f'The bridge has kept the entity enabled — this point is still being polled.\n\n'
             f'Please go to Settings > Entities and re-enable it.\n\n'
             f'To remove a data point from HA use the Nibe Entity Manager card — '
             f'not the HA entity settings.'),
            notif_id,
        )

    # ------------------------------------------------------------------ #
    # Changelog                                                            #
    # ------------------------------------------------------------------ #

    def mark_changelog_read(self) -> None:
        """Mark all changelog entries as read."""
        log_history.info("Marking all changelog entries as read")
        for entry in self.change_history:
            entry['unread'] = False
        self._history_seq       += 1
        self._last_published_seq = self._history_seq
        history_payload = {
            'history':       list(self.change_history),
            'total_entries': len(self.change_history),
            'unread_count':  0,
            'last_updated':  time.time(),
            '_seq':          self._history_seq,
        }
        self.mqtt.publish(
            BrowserTopic.CHANGELOG_HISTORY, _compress_payload(history_payload), retain=True
        )
        self.mqtt.publish(
            BrowserTopic.CHANGELOG_UNREAD,
            json.dumps({'unread_count': 0, 'last_change': time.time()}),
            retain=True,
        )

    def _prune_changelog_if_due(self) -> bool:
        """Prune changelog entries older than changelog_retention_days.

        Runs at most once per hour — the deque's maxlen already prevents
        unbounded growth, so pruning on every insert is unnecessary.
        Returns True if pruning ran (useful for tests).

        Keeps at least 50 entries regardless of age so the changelog is
        never wiped entirely on a long-running installation.
        """
        now = time.time()
        if now - self._last_prune_time < _CHANGELOG_PRUNE_S:
            return False
        self._last_prune_time = now

        retention_days = self.changelog_retention_days
        cutoff_ts      = now - retention_days * 86400

        valid   = [e for e in self.change_history
                   if (isinstance(e, dict)
                       and 'added' in e and 'removed' in e
                       and 'timestamp' in e and 'iso_timestamp' in e)]
        recent  = [e for e in valid if e.get('timestamp', 0) >= cutoff_ts]
        old     = [e for e in valid if e.get('timestamp', 0) <  cutoff_ts]

        needed  = max(0, _CHANGELOG_MIN_ENTRIES - len(recent))
        kept    = recent + old[:needed]

        pruned  = len(self.change_history) - len(kept)
        if pruned > 0:
            self.change_history = deque(kept, maxlen=self.change_history.maxlen)
            log_history.debug("Changelog pruned: removed %d expired entries", pruned)
        return True

    def _update_changelog_history(self, change_event: dict) -> None:
        """Append a change event to the persistent changelog and republish to MQTT."""
        history_entry = {
            'timestamp':     change_event.get('timestamp', time.time()),
            'iso_timestamp': change_event.get('iso_timestamp', _fmt_ts()),
            'added':         change_event.get('added', []),
            'removed':       change_event.get('removed', []),
            'id':            f"change_{int(time.time() * 1000)}",
            'unread':        True,
            'source':        change_event.get('source', 'firmware'),
            'triggered_by':  change_event.get('triggered_by'),
        }
        self.change_history.appendleft(history_entry)
        # Time-based pruning runs at most hourly — the deque hard cap already
        # prevents unbounded growth between prune cycles.
        self._prune_changelog_if_due()

        unread_count = sum(1 for e in self.change_history if e.get('unread', False))

        self._history_seq += 1

        history_payload = {
            'history':       list(self.change_history),
            'total_entries': len(self.change_history),
            'unread_count':  unread_count,
            'last_updated':  time.time(),
            '_seq':          self._history_seq,
        }
        self.mqtt.publish(
            BrowserTopic.CHANGELOG_HISTORY, _compress_payload(history_payload), retain=True
        )
        self.mqtt.publish(
            BrowserTopic.CHANGELOG_UNREAD,
            json.dumps({'unread_count': unread_count, 'last_change': time.time()}),
            retain=True,
        )
        # Update _last_published_seq only after the publish call so that a
        # crash between appendleft and publish leaves _last_published_seq
        # behind _history_seq.  On restart the incoming retained message will
        # not be filtered by the seq guard — the in-memory history starts
        # fresh from the broker's retained copy, which is correct.
        self._last_published_seq = self._history_seq

    def _setup_history_loading(self) -> None:
        """Subscribe to retained changelog topics to reload history on startup."""
        def on_history_message(_client, _userdata, message):
            try:
                if not message.payload:
                    return
                raw  = _decompress_payload(message.payload)
                data = json.loads(raw.decode('utf-8'))
                incoming_seq = data.get('_seq', -1)
                if incoming_seq != -1 and incoming_seq == self._last_published_seq:
                    return
                if 'history' in data and isinstance(data['history'], list):
                    clean_history = deque(maxlen=self.change_history.maxlen)
                    for entry in data['history']:
                        if isinstance(entry, dict):
                            cleaned = {
                                'timestamp':     entry.get('timestamp', time.time()),
                                'iso_timestamp': entry.get('iso_timestamp', _fmt_ts()),
                                'added':         entry.get('added', []),
                                'removed':       entry.get('removed', []),
                                'id':            entry.get('id', f"change_{int(time.time()*1000)}"),
                                'unread':        entry.get('unread', False),
                                'source':        entry.get('source', 'firmware'),
                                'triggered_by':  entry.get('triggered_by'),
                            }
                            if (isinstance(cleaned['added'], list)
                                    and isinstance(cleaned['removed'], list)):
                                clean_history.append(cleaned)
                    self.change_history = clean_history
                    # Force prune on load regardless of last-prune timestamp so
                    # stale entries don't accumulate across restarts with no changes.
                    self._last_prune_time = 0.0
                    self._prune_changelog_if_due()
                    unread = sum(1 for e in self.change_history if e.get('unread', False))
                    log_history.info("Loaded %d historical changes from MQTT", len(self.change_history))
                    if unread > 0:
                        log_history.info("%d unread changes", unread)
            except Exception as e:
                log_history.warning(
                    "Could not load changelog history from MQTT retained message "
                    "(the message may be from an older bridge version): %s", e,
                )
                self.change_history = deque(maxlen=self.change_history.maxlen)

        def on_unread_message(_client, _userdata, message):
            try:
                if not message.payload:
                    return
                data         = json.loads(message.payload.decode('utf-8'))
                unread_count = data.get('unread_count', 0)
                for entry in self.change_history:
                    entry['unread'] = False
                if unread_count > 0 and self.change_history:
                    # deque doesn't support slice notation — convert to list
                    # for the tail operation.  The list is temporary and small.
                    for entry in list(self.change_history)[-unread_count:]:
                        entry['unread'] = True
            except Exception as e:
                log_history.warning(
                    "Could not restore changelog unread state from MQTT: %s", e,
                )

        # Store callbacks so resubscribe_all() can replay them after a broker restart.
        self._on_history_message = on_history_message
        self._on_unread_message  = on_unread_message

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
        payload = _compress_payload(
            json.loads(self.dynamic_point_map.serialise())
        )
        self.mqtt.publish(BrowserTopic.DYNAMIC_MAP, payload, retain=True)
        self.dynamic_point_map.to_file()
        log_discovery.debug(
            "Persisted DynamicPointMap (%d entries)", len(self.dynamic_point_map)
        )

    def _persist_active_dynamic(self) -> None:
        """Persist the active_dynamic_points set to MQTT.

        Called whenever active_dynamic_points changes — on dynamic point
        activation, reconciliation, and disappearance.  The retained message drives
        startup reconciliation: on restart the bridge re-enables exactly the
        points that were active when it shut down.
        """
        payload = json.dumps(sorted(self.active_dynamic_points))
        self.mqtt.publish(BrowserTopic.ACTIVE_DYNAMIC, payload, retain=True)
        log_discovery.debug(
            "Persisted %d active dynamic point(s)", len(self.active_dynamic_points)
        )

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
            with open(path, 'w', encoding='utf-8') as f:
                f.write(mode_name)
        except OSError as e:
            log_restore.warning("Could not write applied-mode fallback file: %s", e)
        self.mqtt.publish(BrowserTopic.APPLIED_MODE, mode_name, retain=True)
        log_restore.debug("Persisted applied mode: %s", mode_name)

    def _read_applied_mode_from_file(self, path: str | None = None) -> str | None:
        """Read the last-applied mode from the file fallback. None if absent/unreadable.

        See _persist_applied_mode for why path resolves dynamically
        instead of using a plain default argument.
        """
        if path is None:
            path = _APPLIED_MODE_FILE
        try:
            with open(path, 'r', encoding='utf-8') as f:
                mode = f.read().strip()
            return mode or None
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

        def on_message(_client, _userdata, message):
            if message.payload:
                try:
                    result[0] = message.payload.decode('utf-8').strip() or None
                except Exception:
                    result[0] = None
            received.set()

        self.mqtt.subscribe(BrowserTopic.APPLIED_MODE)
        self.mqtt.message_callback_add(BrowserTopic.APPLIED_MODE, on_message)
        try:
            received.wait(timeout=_APPLIED_MODE_TIMEOUT_S)
        finally:
            self.mqtt.message_callback_remove(BrowserTopic.APPLIED_MODE)
            self.mqtt.unsubscribe(BrowserTopic.APPLIED_MODE)

        if result[0] is not None:
            return result[0]
        return self._read_applied_mode_from_file()

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

        current_raw_values = {
            pid: bd['raw_value']
            for pid, bd in self.bulk_data.items()
        }
        bulk_present   = set(self.bulk_data.keys())
        expected_active = self.dynamic_point_map.expected_active_dynamic_points(
            current_raw_values
        )
        persisted_active = set(self.active_dynamic_points)  # snapshot

        activated = 0
        removed   = 0

        # Case 1 + 2: process expected active set
        for point_id in expected_active:
            if point_id in bulk_present:
                # Point is expected and present — ensure it is active
                if point_id not in self.mqtt_enabled_points:
                    point_data = self.bulk_data.get(point_id, {})
                    metadata   = point_data.get('metadata', {})
                    title      = point_data.get('title', f'Point {point_id}')
                    description = point_data.get('description', '')
                    entity_type, category = self._get_cached_entity_type({
                        'variableId':  point_id,
                        'metadata':    metadata,
                        'title':       title,
                        'description': description,
                    })
                    self._index_point({
                        'variableId':      point_id,
                        'display_title':   title,
                        'description':     description,
                        'metadata':        metadata,
                        'entity_type':     entity_type,
                        'entity_category': category,
                        'is_writable':     metadata.get('isWritable', False),
                        'is_dynamic':      True,
                    })
                    self.enable_entity(point_id)
                    self.active_dynamic_points.add(point_id)
                    activated += 1
                    log_discovery.debug(
                        "Reconcile: activated dynamic point %d '%s'",
                        point_id, title,
                    )
                else:
                    # Already enabled — still need to publish online and
                    # current state so HA doesn't show the entity as unavailable.
                    entity_info = self.active_entities_by_id.get(point_id)
                    if entity_info:
                        self.mqtt.publish(
                            entity_info['availability_topic'], "online", retain=True
                        )
                        self._update_entity_state(entity_info)
                    self.active_dynamic_points.add(point_id)
            else:
                # Point is expected but absent from bulk fetch
                if point_id in self.active_dynamic_points:
                    self._deindex_point(point_id)
                    self._pub.invalidate_config_hash(point_id)
                    self.mqtt.publish(
                        BrowserTopic.META_TEMPLATE.format(id=point_id), "", retain=True
                    )
                    if point_id in self.mqtt_enabled_points:
                        self.disable_entity(point_id)
                    self.active_dynamic_points.discard(point_id)
                    removed += 1
                    log_discovery.info(
                        "Reconcile: removed stale dynamic point %d "
                        "(expected but absent from bulk — firmware update?)",
                        point_id,
                    )

        # Case 3: stale persisted entries not in expected set
        stale = persisted_active - expected_active
        for point_id in stale:
            self._deindex_point(point_id)
            self._pub.invalidate_config_hash(point_id)
            self.mqtt.publish(
                BrowserTopic.META_TEMPLATE.format(id=point_id), "", retain=True
            )
            if point_id in self.mqtt_enabled_points:
                self.disable_entity(point_id)
            self.active_dynamic_points.discard(point_id)
            removed += 1
            log_discovery.info(
                "Reconcile: removed stale dynamic point %d "
                "(in persisted active but not in expected — controlling switch changed?)",
                point_id,
            )

        if activated or removed:
            self._persist_active_dynamic()
            self._persist_dynamic_map()
            self.publish_enabled_state()

        log_discovery.info(
            "Startup reconciliation: %d dynamic point(s) activated, %d removed",
            activated, removed,
        )

    def _setup_dynamic_map_loading(self) -> None:
        """Subscribe to retained MQTT topics to restore dynamic state on startup.

        Two topics are loaded:
        1. DYNAMIC_MAP  — the full DynamicPointMap table.
        2. ACTIVE_DYNAMIC — the set of currently active dynamic point_ids.

        Both are only acted upon before initial_discovery_complete is True.
        After that point the in-memory state is authoritative and retained
        message re-deliveries (triggered by our own publishes) are ignored.
        """
        def on_dynamic_map_message(_client, _userdata, message):
            if self.initial_discovery_complete:
                return
            try:
                if not message.payload:
                    return
                raw = message.payload.decode('utf-8')
                # Decompress if gzip-encoded
                if raw.startswith(_GZIP_SENTINEL):
                    import base64
                    data = json.loads(
                        gzip.decompress(base64.b64decode(raw[len(_GZIP_SENTINEL):])).decode('utf-8')
                    )
                    json_str = json.dumps(data)
                else:
                    json_str = raw
                count = self.dynamic_point_map.deserialise(json_str)
                log_discovery.info(
                    "Restored DynamicPointMap from MQTT: %d entries", count
                )
            except Exception as e:
                log_discovery.warning(
                    "Could not restore DynamicPointMap from MQTT — "
                    "will try file fallback or start fresh: %s", e
                )

        def on_active_dynamic_message(_client, _userdata, message):
            if self.initial_discovery_complete:
                return
            try:
                if not message.payload:
                    return
                point_ids = json.loads(message.payload.decode('utf-8'))
                if not isinstance(point_ids, list):
                    return
                loaded = {int(pid) for pid in point_ids}
                self.active_dynamic_points.update(loaded)
                log_discovery.info(
                    "Restored %d active dynamic point(s) from MQTT: %s",
                    len(loaded), sorted(loaded),
                )
            except Exception as e:
                log_discovery.warning(
                    "Could not restore active_dynamic_points from MQTT: %s", e
                )

        self._on_dynamic_map_message    = on_dynamic_map_message
        self._on_active_dynamic_message = on_active_dynamic_message

        self.mqtt.subscribe(BrowserTopic.DYNAMIC_MAP)
        self.mqtt.message_callback_add(BrowserTopic.DYNAMIC_MAP, on_dynamic_map_message)
        self.mqtt.subscribe(BrowserTopic.ACTIVE_DYNAMIC)
        self.mqtt.message_callback_add(BrowserTopic.ACTIVE_DYNAMIC, on_active_dynamic_message)

    def get_memory_usage(self) -> dict[str, int]:
        """Return memory usage statistics for debugging and monitoring.
        
        Returns
        -------
        dict[str, int]
            Dictionary with memory usage metrics including:
            - total_points: Total number of points tracked
            - active_entities: Number of active entities
            - cache_sizes: Sizes of various caches
            - estimated_memory_mb: Estimated memory usage in MB
        """
        
        stats = {
            'total_points':            len(self.all_points_by_id),
            'active_entities':         len(self.active_entities_by_id),
            'mqtt_enabled_points':     len(self.mqtt_enabled_points),
            'active_dynamic_points':   len(self.active_dynamic_points),
            'value_cache_size':        len(self.value_cache._cache),
            'last_states_size':        len(self.last_states),
            'point_string_cache_size': len(self._point_string_cache),
            'pending_writes':          len(self.pending_writes),
        }

        estimated_bytes = (
            stats['total_points'] * 100 +
            stats['active_entities'] * 500 +
            sum(stats[f'{cache}_size'] for cache in ['value_cache', 'last_states', 'point_string_cache']) * 50
        )
        stats['estimated_memory_mb'] = round(estimated_bytes / (1024 * 1024), 2)  # type: ignore[assignment]

        try:
            stats['actual_object_size_mb'] = round(sys.getsizeof(self) / (1024 * 1024), 2)  # type: ignore[assignment]
        except Exception:
            stats['actual_object_size_mb'] = None

        return stats

    def _get_cached_entity_type(self, point_data: dict) -> tuple[str, str]:
        """Return the (entity_type, category) for a point, using a cache.

        Metadata and title are static within a single process run — a firmware
        update that changes them will restart the bridge, clearing the cache.
        The key is therefore just point_id; no complex compound key needed.
        """
        point_id = point_data['variableId']
        cached = self._entity_type_cache.get(point_id)
        if cached is not None:
            return cached
        result = detect_entity_type(point_data)
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
            with open(path, 'r', encoding='utf-8') as f:
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
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(snapshots, f, indent=2)
        except OSError as e:
            log_restore.warning("Could not write snapshots file: %s", e)
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

        import time as _time
        snapshots = self._load_snapshots(path=path)

        # Replace existing snapshot with the same name
        snapshots = [s for s in snapshots if s.get('name') != name]

        if len(snapshots) >= _SNAPSHOTS_MAX:
            return False, (
                f"Maximum of {_SNAPSHOTS_MAX} snapshots reached. "
                "Delete one before saving a new snapshot."
            )

        snapshot = {
            'name':        name,
            'timestamp':   _time.strftime('%Y-%m-%d %H:%M:%S'),
            'point_ids':   sorted(self.mqtt_enabled_points),
            'point_count': len(self.mqtt_enabled_points),
            'mode':        self._read_applied_mode_from_file() or 'unknown',
        }
        snapshots.append(snapshot)
        self._save_snapshots(snapshots, path=path)
        log_restore.info(
            "Snapshot '%s' saved: %d points", name, len(self.mqtt_enabled_points)
        )
        return True, f"Snapshot '{name}' saved ({len(self.mqtt_enabled_points)} points)."

    def restore_snapshot(self, name: str, mode: str = 'flush', path: str | None = None) -> tuple[bool, str]:
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
        snapshots = self._load_snapshots(path=path)
        snapshot  = next((s for s in snapshots if s.get('name') == name), None)
        if snapshot is None:
            return False, f"Snapshot '{name}' not found."

        # Restoring into menus or all mode would conflict with the system-managed
        # entity set — the mode re-applies on restart and overwrites the restored
        # selection. Block restore and ask the user to switch to a manual mode first.
        current_mode = self._read_applied_mode_from_file() or ''
        if current_mode in ('menus', 'all'):
            return False, (
                f"Cannot restore a snapshot while in '{current_mode}' mode. "
                "Switch to 'essential', 'monitoring', 'advanced', or 'none' first, "
                "then restore."
            )

        saved_ids    = set(snapshot.get('point_ids', []))
        firmware_ids = set(self.all_points_by_id.keys())
        valid_ids    = saved_ids & firmware_ids
        missing      = saved_ids - firmware_ids

        if missing:
            log_restore.warning(
                "Snapshot '%s': %d point(s) no longer in firmware — skipped: %s",
                name, len(missing), sorted(missing)[:10],
            )

        protected = set(self.active_dynamic_points)

        with self._suppress_lock:
            was_suppressed = self._suppress_enabled_state_depth > 0
            if not was_suppressed:
                self._suppress_enabled_state_depth += 1

        try:
            if mode == 'flush':
                # Disable everything not in the snapshot (protect dynamic points)
                to_disable = (self.mqtt_enabled_points - valid_ids) - protected
                for pid in to_disable:
                    self.disable_entity(pid)

            # Enable all valid snapshot points not already enabled
            to_enable = valid_ids - self.mqtt_enabled_points
            for pid in to_enable:
                self.enable_entity(pid)
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
        log_restore.info("Snapshot '%s' restored (mode=%s): %s", name, mode, msg)
        return True, msg

    def delete_snapshot(self, name: str, path: str | None = None) -> tuple[bool, str]:
        """Delete a named snapshot."""
        snapshots = self._load_snapshots(path=path)
        filtered  = [s for s in snapshots if s.get('name') != name]
        if len(filtered) == len(snapshots):
            return False, f"Snapshot '{name}' not found."
        self._save_snapshots(filtered, path=path)
        log_restore.info("Snapshot '%s' deleted", name)
        return True, f"Snapshot '{name}' deleted."

    def publish_snapshots(self) -> None:
        """Publish current snapshot list to the MQTT browser topic."""
        snapshots = self._load_snapshots()
        self.mqtt.publish(
            BrowserTopic.SNAPSHOTS, json.dumps(snapshots), retain=True
        )


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
    product    = api_response.get("product", {})
    api_name   = (product.get("name") or "").strip()
    model_name = api_name or "Nibe S-series"

    # Use the config device_name as-is — it is always the user's explicit
    # choice.  If the API provides a name and the config is still the
    # shipped default, prefer the API name so it reflects the actual
    # hardware without requiring manual configuration.
    _DEFAULT_DEVICE_NAME = "Nibe SMO S40"
    if device_name == _DEFAULT_DEVICE_NAME and api_name:
        resolved_name = api_name
    else:
        resolved_name = device_name

    device_root = base_url.rsplit('/api/v1/devices/', 1)[0]
    device = {
        "identifiers":       [device_id],
        "name":              resolved_name,
        "manufacturer":      product.get("manufacturer", "NIBE"),
        "model":             model_name,
        "model_id":          product.get("firmwareId", ""),
        "serial_number":     product.get("serialNumber", ""),
        "configuration_url": device_root,
    }
    return {k: v for k, v in device.items() if v != ""}
