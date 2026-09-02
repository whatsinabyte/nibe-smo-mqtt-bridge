# Architecture — Nibe S-Series MQTT Bridge

Developer reference. Not user documentation — see [DOCS.md](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/blob/main/nibe_s_series/DOCS.md) for installation and configuration.

---

## 1. Overview

The bridge is a long-running Python process that sits between a Nibe S-series heat pump controller and Home Assistant. It polls the controller's local REST API on a configurable interval (15–300 seconds), translates the raw register data into HA-native MQTT discovery configs, and publishes state updates. HA treats the result as native entities — no YAML configuration, no custom integration, no cloud dependency.

Data flows in one direction by default: controller → bridge → MQTT broker → HA. Write commands travel the reverse path: HA UI → MQTT command topic → bridge → controller REST API → bridge publishes confirmed state.

The bridge also provisions Lovelace dashboards and a companion card via the HA WebSocket API, and surfaces management controls (mode switching, alarm reset, test suite) as HA entities on a dedicated management device.

---

## 2. System diagram

![System diagram](https://raw.githubusercontent.com/whatsinabyte/nibe-smo-mqtt-bridge/main/docs/nibe-bridge-simple.svg)

---

## 3. Threading model

The bridge runs five concurrent execution contexts:

| Context | What runs there |
|---|---|
| **Main thread** | Poll loop — `_poll_loop()` calls `update_all_states()` every `bulk_interval` seconds |
| **Paho network thread** | All MQTT callbacks (`on_connect`, `on_message`, `on_disconnect`) |
| **Write executor** | `ThreadPoolExecutor(max_workers=1)` — serialises all write commands to the controller |
| **Management executor** | `ThreadPoolExecutor(max_workers=2)` — handles management button presses (test suite, alarm reset, force poll) |
| **Registry watcher thread** | Daemon thread — holds a long-lived WebSocket to HA Core for entity registry events |
| **Lovelace thread** | Daemon thread — dashboard provisioning at startup; exits after completion |

**Key invariants:**
- All writes to the controller are serialised through the single-worker write executor. This prevents concurrent PATCH requests from racing.
- MQTT callbacks never block — heavy work (write commands, management actions) is always dispatched to an executor.
- The main thread and paho network thread share `EntityManager` state. Accesses that must be atomic use the locks already embedded in the data structures (`threading.Lock` on the point registry dict).
- The registry watcher holds no reference to the paho client — all MQTT publishing from registry events goes through `EntityManager`, which holds the client reference.

**Locks in use:**

| Lock | Owner | Guards |
|---|---|---|
| `EntityManager._em_lock` | `nibe_entity_manager.py` | `last_states` writes from the poll loop and the write executor |
| `EntityManager._post_write_lock` | `nibe_entity_manager.py` | `post_write_active` / `_post_write_controlling_point` / `_post_write_until`, read-modified across the poll loop and write executor |
| `ValueCache` / `LRUCache` internal locks | `nibe_caching.py` | `_point_string_cache` / `_entity_type_cache`, mutated from both the poll thread and the write/management executors |
| `HAEntityRegistryWatcher._registry_map_lock` | `nibe_ha_integration.py` | `_unique_id_map`, read by callbacks dispatched off the registry watcher's WebSocket thread while the watcher itself mutates it |
| `_regen_timer_lock` (closure-local) | `nibe_lovelace.py`, `_on_enabled_state_change_factory` | the debounce `threading.Timer` reference, to prevent concurrent enable/disable events from orphaning a timer |

---

## 4. Module reference

### 4.1 `generate_nibe_mqtt.py` — entrypoint

The orchestration layer. Owns configuration parsing, startup sequencing, the poll loop, and clean shutdown. Contains no business logic.

`main()` (~70 lines) delegates to four named functions:

- `_build_infrastructure(cfg)` — creates the API client and MQTT client, connects to the broker, and returns a `set_entity_manager` closure used to wire the entity manager into MQTT `on_connect` after it is constructed
- `_run_startup_sequence(...)` — assembles all subsystems, runs discovery and MQTT scan, applies the initial mode, starts threads, publishes snapshots
- `_poll_loop(em, pub, initial_mode)` — runs until `KeyboardInterrupt`; implements exponential backoff (5s × N, capped at 60s) and a bridge alert after 5 consecutive failures
- `_shutdown(...)` — drains executors with a 35-second hard timeout, publishes offline status, optionally clears retained MQTT topics (`NIBE_REMOVE_FRONTEND=1`)

**Configuration priority** (highest → lowest): CLI flags → environment variables (non-credential settings only, e.g. `NIBE_MODE`, `NIBE_LANGUAGE`, `NIBE_MODE_SWITCH_BEHAVIOR`) → `/data/options.json` → `/config/secrets.yaml` or `/homeassistant/secrets.yaml` → defaults.

**Device identity persistence:** `_derive_device_id()` builds the HA device identifier from the controller's serial number (`nibe_<serial>`) and persists it to `/data/device_id`. A later startup where the device is transiently unreachable (serial not available in that startup's API response) reuses the persisted id rather than falling back to the generic config default — without this, device_id flip-flops across restarts depending on whether that specific connection attempt happened to succeed, and every entity gets recreated under a different HA device identity each time, most visibly the Management device (published unconditionally at every startup regardless of whether point discovery succeeds), leaving the old one behind as an orphaned empty duplicate device in HA.

**What this module does not do:** no entity type logic, no MQTT topic construction, no Nibe API calls outside of the startup sequence wiring.

---

### 4.2 `nibe_api.py` — NibeApiClient

All HTTP communication with the controller. Nothing in this module knows about MQTT, HA, or entity types. Callers receive plain Python dicts/booleans.

**TLS:** the controller uses a self-signed certificate. The client creates an `ssl.SSLContext` with verification disabled — there is no CA chain available for the controller's cert.

**Authentication:** HTTP Basic Auth, Base64-encoded, sent as an `Authorization` header on every request.

**Retry policy:** one automatic retry on transient errors with full-jitter backoff (`random.uniform(0, min(2.0s, 10.0s))`). Full jitter prevents correlated retries if multiple callers hit the same transient failure simultaneously.

**Known firmware deviations from the documented API spec:**
1. `GET /points` returns `"value"` where the spec shows `"datavalue"`
2. `PATCH /points` returns the full point object rather than `{"variableId": "modified"}`
3. `GET /points/{id}` returns HTTP 404 for inactive dynamic points (undocumented — treated as `None` return)

**`write_device_mode()`** uses lowercase JSON body keys (`"aidmode"` / `"smartmode"`) matching the URL path segment. This is confirmed working on firmware 4.12.6 — the spec is ambiguous about case.

**What this module does not do:** no MQTT, no entity management, no HA notifications.

---

### 4.3 `nibe_entity_detection.py` — pure classification functions

Stateless pure functions and lookup tables. No I/O, no state, no imports from the rest of the bridge. Every function is trivially unit-testable in isolation.

**Entity type detection strategy:** the firmware metadata is too ambiguous for reliable auto-detection. `ENTITY_TYPE_OVERRIDES` is the authoritative override table. `binary_sensor` in particular cannot be auto-detected — a point is only classified as `binary_sensor` after developer confirmation in the HA UI, because the firmware provides no reliable signal to distinguish a two-state boolean from a two-value enum.

**Divisor handling:** all firmware values are integers. `divisor` converts them to display values (`raw / divisor`). `divisor: 0` is treated as 1 (firmware quirk). Only four divisors appear in practice: 1, 10, 60, 100.

**Enum-description parsing:** firmware `description` strings encoding value→label mappings (e.g. `"0 = Off, 1 = Active"`) aren't consistent about separator — most registers use `'='`, some use `':'` instead, and at least one mixes both within the same string. `_split_mapping_part()` tries `'='` then `':'` per comma-separated segment (not per whole string) so a mixed description parses every pair correctly. Both `parse_description_mapping()` (value display) and `_detect_holding_entity()`'s `select`-vs-`number` classification call through this shared helper — they used to be two independent ad-hoc `'='`-only checks, which meant a colon-only register's *value* could translate correctly while its *entity type* still didn't.

**`clean_string()`** strips `U+00AD` soft-hyphens that the firmware embeds in register titles. These are invisible in most editors but corrupt entity IDs if not removed.

**Mode sets** (`ESSENTIAL_POINTS`, `MONITORING_POINTS`, etc.) are `frozenset[int]` literals. `all` uses `None` as sentinel, replaced at runtime with the full discovered point list. `none` is an empty frozenset.

**What this module does not do:** no I/O, no state, no side effects of any kind.

---

### 4.4 `nibe_entity_manager.py` — EntityManager

The largest module (~3,250 lines) and the core of the bridge. Owns the full lifecycle of every data point as a HA entity.

**Point registry:** all discovered points are indexed by `variableId` in a dict. The registry is populated once at startup via `discover_points()` and is read-only thereafter (dynamic points are handled separately).

**MQTT-first state:** retained discovery configs in the broker are the single source of truth for which entities are enabled. On restart, `scan_mqtt_discovery()` reads these back rather than keeping a separate state file. This means the bridge survives restarts without losing user customisations.

**Value cache (`ValueCache`, defined in `nibe_caching.py`):** suppresses redundant MQTT state publishes via two guards:
- Change threshold: floating-point values within a small epsilon of the previous published value are not republished
- Minimum interval: a value that has not changed is still republished after a configurable interval to keep HA's "last changed" timestamp meaningful

**LRU cache (`LRUCache`, defined in `nibe_caching.py`):** entity type classifications and point string representations are cached in an `LRUCache(max_size=2000)` to avoid recomputing on every poll cycle. Both caches were extracted out of this module into `nibe_caching.py` — see §4.4a.

**Pending write guard:** when a write command is dispatched, the affected point is marked "pending". State publishes for that point are suppressed until the API confirms the write or the guard times out. This prevents the HA UI from flipping back to the old value while the write is in flight. `optimistic: false` is set on all writable discovery configs for the same reason.

**Snapshot save/restore:** up to 10 named snapshots of the enabled entity set are persisted to `/data/snapshots.json`. Restore is blocked in `menus` and `all` mode (too many entities to restore safely). Flush mode replaces the entire enabled set; merge mode adds to it. This restore-time flush/merge choice is independent of the `mode_switch_behavior` config option below — the two operate on different triggers (a snapshot restore action vs. an actual mode change) and don't interact.

**Mode reconciliation (`apply_mode()`):** called only on a fresh install or a detected mode change across a restart (never on an ordinary same-mode restart, so manual Entity Manager additions survive normal restarts). Reconciles the enabled set to the target mode's points, protecting active dynamic points from ever being disabled by a mode change — a dynamic point's presence is firmware-state-driven, not mode-driven, so switching modes never kills a live dynamic entity, and a dynamic point's appearance is never suppressed by the current mode either (only `mqtt_enabled_points` minus `protected` is disabled; new dynamic points are enabled unconditionally regardless of mode). Whether points outside the new mode's set get disabled at all is governed by the `mode_switch_behavior` config option: `replace` (default) disables them; `merge` never disables anything, only adds the new mode's points.

**Wanted points (`_wanted_points`, a reactive safety net):** complements `dynamic_point_map`'s predictive, controller-driven tracking (which only ever registers writable switch/select points as candidate controllers — an unbounded numeric register can't safely be probed exhaustively in learning mode). A point that's dynamically tracked but not itself a switch/select (e.g. a `number`) can only ever be linked to its controller via the narrow post-write-scan-window path; if that window is missed — most commonly because the underlying value was changed directly on the controller rather than through HA — the point permanently falls back to being treated as an ordinary static point, and the generic "absent from bulk data" disable (`_update_entity_state`) has no memory of it ever having been wanted. `_wanted_points: set[int]` (persisted to the retained `BrowserTopic.WANTED_POINTS` MQTT topic plus a `/data/wanted_points.json` file fallback) is a causality-agnostic set of every point explicitly enabled by the user via `enable_entity()`, `apply_mode()`, or `restore_snapshot()` — never by the dynamic-tracking machinery itself, or it would fight that mechanism's own lifecycle. `_reconcile_wanted_points()` runs after every bulk fetch and re-enables any wanted point that has reappeared but isn't currently enabled. A mode switch or snapshot flush explicitly un-marks the points it disables (an intentional override); the generic "absent from bulk data" disable explicitly does not (`remove_from_wanted=False`) — that's the whole point of the mechanism.

**`baseline_point_ids` and re-learnability:** a point that first appears outside a post-write scan window, before its real controlling switch/select has ever been learned, gets indexed as a plain static point (`is_dynamic: False`) and added to `baseline_point_ids` rather than auto-enabled. The appearance-detection guard in `_fetch_bulk_data()` requires `point_id not in self.baseline_point_ids`, so `_update_entity_state`'s generic disable fallback discards the point from `baseline_point_ids` whenever it disables it (mirroring what the post-write-scan disappearance branch already did) — without this, a point that ever took this path would be permanently stuck unable to be routed through the dynamic-learning path again, even after a correct, HA-driven write to its real controller reopens a legitimate scan window.

**Two functions that must not be refactored speculatively:**
- `_fetch_bulk_data()` (~297 lines): the complexity is inherent. It simultaneously manages the string cache, bulk data mutation, new-point classification, baseline tracking, and disappeared-set computation. Extracting sub-functions is feasible but high-risk for a fully-covered function with no current bug.
- `_publish_dynamic_changes()` (~215 lines): same rationale. Handles the full causal chain from point appearance/disappearance through DynamicPointMap update, HA notification, changelog entry, and dashboard regen scheduling.

**What this module does not do:** no raw HTTP, no MQTT topic string construction, no HA registry watching, no notification sending.

The MQTT protocol between the bridge and the Entity Manager card is documented in [`docs/card-api.md`](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/blob/main/docs/card-api.md).

---

### 4.4a `nibe_caching.py` — ValueCache, LRUCache

Extracted from `nibe_entity_manager.py`. Two small, independent, generic cache classes with no knowledge of Nibe/HA/MQTT concepts.

**`ValueCache`:** the change-threshold and minimum-republish-interval logic described in §4.4.

**`LRUCache`:** a bounded-size, thread-safe least-recently-used cache. Each public method acquires an internal `threading.Lock` — required because `EntityManager` reads and writes it from both the poll thread and the write/management executor threads.

**What this module does not do:** no Nibe/HA/MQTT-specific logic — both classes are generic and reusable.

---

### 4.5 `nibe_mqtt_publisher.py` — MqttDiscoveryPublisher

Single source of truth for all MQTT topic strings and discovery config payloads. Builds and publishes HA MQTT discovery configs for every entity type the bridge supports.

**Topic ownership:** all fixed topic strings are defined as class attributes on `MgmtTopic` and `BrowserTopic` enums. No topic string is constructed outside this module.

**Discovery config structure:** each entity type (sensor, binary_sensor, switch, select, number, button) has a dedicated builder — now living in `nibe_discovery_config.py` (§4.5a) — that populates the mandatory and optional HA discovery fields. The builders use `optimistic: false` on all writable types to prevent HA from optimistically reflecting the command before the bridge confirms the write. This module calls those builders and owns publishing the results.

**Debug-only entities:** `Flush Dynamic Map`, `Run Test Suite`, `Test Suite Result`, `Test API Connection`, and `Connectivity Check Result` are only published when `debug_mode=True`. When `debug_mode=False`, empty retained payloads are sent to those discovery topics so HA removes the entities. A full HA restart is required for the entity registry to reflect the removal.

**What this module does not do:** no HTTP, no entity lifecycle tracking, no threading.

For the full topic list and JSON schemas for every topic this module publishes, see [`docs/card-api.md`](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/blob/main/docs/card-api.md).

---

### 4.5a `nibe_discovery_config.py` — pure discovery config builders

Extracted from `nibe_mqtt_publisher.py`. Pure functions that build HA MQTT discovery config dicts — one builder per entity type (sensor, binary_sensor, switch, select, number, button).

**Why extracted:** keeps config-building testable in isolation from MQTT I/O, and keeps `nibe_mqtt_publisher.py` focused on publishing.

**Circular-import avoidance:** builders take topic strings as parameters rather than importing `EntityManager` or constructing topics themselves, since `MgmtTopic`/`BrowserTopic` (owned by `nibe_mqtt_publisher.py`) are the only source of topic strings project-wide.

**What this module does not do:** no MQTT publishing, no I/O of any kind.

---

### 4.6 `nibe_dynamic_map.py` — DynamicPointMap

A causal table recording which writable switch/select points cause dynamic points to appear or disappear in the firmware's bulk fetch response.

**Why this exists:** some Nibe operating modes expose extra registers only while active (e.g. manual-mode setpoints appear when the heating curve is switched to manual). Without the map, the bridge can only detect these by comparing two consecutive bulk fetches — slow and unreliable. With the map, a write to a known controlling point immediately triggers a targeted probe of its known dynamic points via `fetch_point()` rather than waiting for the next bulk cycle.

**Causal learning:** the map self-populates. On first observation of a write to a switch/select point, the bridge records the outcome (which dynamic points appeared or disappeared) as a `DynamicPointEntry`. Subsequent writes to the same point bypass the detection cycle entirely and apply the known outcome immediately.

**`mark_absent_as_firmware_removed(bulk_point_ids)`:** the counterpart to `restore_from_bulk` — when a previously known dynamic point stops appearing in the bulk fetch response, it is marked as firmware-removed rather than left in a stale "active" state. Wired into `EntityManager.discover_points()`.

**Persistence:** the map is serialised to JSON and published to a retained MQTT topic (`nibe/browser/dynamic_point_map`) so it survives restarts. The module has no I/O of its own — all persistence is delegated to `EntityManager._persist_dynamic_map()`.

**What this module does not do:** no I/O, no MQTT, no HTTP. Pure data structure with JSON serialisation.

---

### 4.7 `nibe_ha_integration.py` — HA integration layer

Everything that talks to HA itself rather than to the Nibe device or the MQTT broker.

**`notify_ha()` / `dismiss_ha()`:** create and clear HA persistent notifications via the Supervisor REST API (`http://supervisor/core/api/services/notify`). Notification links use absolute URLs constructed by `_get_ha_base_url()` — relative `/local/` paths are intercepted by the HA frontend router and do not navigate correctly from notification cards.

**`_get_ha_base_url()`:** fetches `internal_url` / `external_url` from `GET http://supervisor/core/api/config`. Result is cached in a module-level global after first fetch. Returns `''` on failure so callers always get a string.

**`_get_ha_language()`:** same endpoint, cache/retry pattern, and empty-string-on-failure contract as `_get_ha_base_url()` above, but reads the `language` field instead — used by `load_config()` in `generate_nibe_mqtt.py` to auto-detect the Nibe REST API query language (the `language` config option) when it's left at `"auto"`. Deliberately a separate request/cache from `_get_ha_base_url()` rather than sharing one, so a failure or retry cooldown in one doesn't couple to the other.

**`HAEntityRegistryWatcher`:** a long-lived WebSocket subscriber to `ws://supervisor/core/websocket`. Subscribes to `entity_registry_updated` events and maintains a local cache of `unique_id → entity_id` mappings in `_unique_id_map`, guarded by `_registry_map_lock` (§3) since it is read from callbacks dispatched off the watcher's own WebSocket thread while the watcher mutates it. This replaces the previously required companion HA automation. The watcher handles the known HA behaviour where MQTT entity create events omit `unique_id` from the event payload — on such events it triggers an asynchronous full registry refresh. `_on_entity_enabled()` / `_on_entity_disabled()` take a lock-protected snapshot of the map before resolving a point, rather than reading the live (potentially concurrently-mutated) dict.

**Management command handlers:** `ManagementCommandHandler` subscribes to management MQTT topics published by the card and HA buttons. Duplicate button presses while a test run is in flight are dropped silently via a `threading.Event` guard. Exit codes: `0` = passed, `-1` = timed out, `-2` = launch error, other = failed. The actual test-suite execution is delegated to `run_test_suite()` in `nibe_test_runner.py` (§4.7a).

**What this module does not do:** no Nibe API calls, no discovery config publishing, no entity lifecycle management.

---

### 4.7a `nibe_test_runner.py` — run_test_suite

Extracted from the `_handle_run_tests` closure in `nibe_ha_integration.py`. `run_test_suite(mqtt_client, notify_fn, dismiss_fn, get_base_url_fn, done_event)` runs the bridge's own pytest suite on demand (triggered by the "Run Test Suite" management button) and publishes the result.

**Dependency injection:** takes `notify_fn` / `dismiss_fn` / `get_base_url_fn` as parameters rather than importing `notify_ha` / `dismiss_ha` / `_get_ha_base_url` directly, avoiding a circular import back into `nibe_ha_integration.py`.

**What this module does not do:** no MQTT topic construction, no entity lifecycle management.

---

### 4.7b `nibe_connectivity_check.py` — run_connectivity_check

`run_connectivity_check(host, base_url, ca_cert_path, auth_header)` runs an independent `ping` + `curl` diagnostic against the configured Nibe controller, triggered by the "Test API Connection" management button (`ManagementCommandHandler._handle_test_connection`). Added to make "the bridge can't reach the device" reports diagnosable without needing SSH/terminal access to the HA host.

**Deliberately independent of `NibeApiClient`/`urllib`:** shells out to `ping`/`curl` rather than reusing the bridge's own HTTP client, so the check shares no code, bug, or misconfiguration with it — the result can genuinely distinguish "a bug in our client" from "a real network/TLS/credentials problem". When `auth_header` is supplied (the bridge's real, configured `Authorization` value) and a CA cert path is configured, the check exercises the exact same TLS verification mode and credentials the bridge itself uses, so it can tell a network failure, a TLS/CA failure, and a credentials failure (HTTP 401/403) apart from each other in one run.

**What this module does not do:** no MQTT publishing, no HA notifications (`ManagementCommandHandler` handles reporting the result, same separation as `nibe_test_runner.py`).

---

### 4.8 `nibe_lovelace.py` — Lovelace provisioning

All interaction with the HA frontend. Creates and maintains two dashboards and the companion card resource registration.

**Nibe Bridge dashboard:** created once on first start. Never overwritten — if the user deletes it, it is recreated on the next restart. Contains the Entity Manager card and management controls.

**Nibe Menus dashboard:** rebuilt from scratch on every restart (and on-demand via "Regenerate Dashboard"). Mirrors the full SMO S40 installer menu structure from `menu_structure.yaml` (163 menus, ~350 settings). Dynamic points are injected below their controlling switch when active and removed when inactive. Only ever built when `mode == 'menus'` — `EntityManager._publish_dynamic_changes()`'s HA notification checks the currently applied mode (`_read_applied_mode_from_file() == 'menus'`) before wording itself around this dashboard, falling back to pointing at the Nibe Bridge dashboard (provisioned in every mode) otherwise.

**Dashboard creation guard:** the check for whether the dashboard already exists requires a successful `lovelace/dashboards` list call. A failed list call is distinguished from "zero dashboards exist" — a failed call does not proceed to a creation attempt that would always fail.

**Regen debounce:** menu dashboard regeneration is debounced — rapid enable/disable operations queue a single regen rather than triggering one per entity change. The debounce is wired into `EntityManager` via a callback registered by `schedule_menu_dashboard_regen()`. The pending `threading.Timer` reference is guarded by `_regen_timer_lock` (§3) so that a cancel-then-replace under concurrent enable/disable events can never orphan a timer (the previous timer firing after being "cancelled" by a racing thread).

**Retry logic:** dashboard regen retries up to 3 times at 3-second intervals when active dynamic point entity IDs are not yet in the HA entity registry, handling the race between MQTT discovery processing and dashboard build. The registry-stability wait is a named helper, `_wait_for_registry_stable()`, extracted from `_setup_menu_dashboard`.

**`_ws_call()` timeout:** each WebSocket request/response call is bounded by an overall deadline (`timeout` seconds from the call, not per-`recv()`). Earlier versions reset a fresh per-`recv()` timeout on every loop iteration, which meant a peer trickling unrelated messages could keep the call alive far past its nominal timeout; the deadline now shrinks with elapsed time and the call gives up once it passes.

**Debug-only view:** an "Unplaced settings" tab is appended to the Nibe Menus dashboard only when `debug_mode=True`. It shows all firmware points not yet documented in `menu_structure.yaml`, grouped into writable/review, writable/series, and read-only sections.

**What this module does not do:** no Nibe API calls, no MQTT publishing outside of resource registration, no entity lifecycle management.

---

### 4.9 `nibe_utils.py` — shared utilities

One function: `fmt_ts(ts)` — formats a Unix timestamp as a human-readable string. No I/O, no state, no imports from the rest of the bridge. Exists to avoid duplicating the formatting logic across modules.

---

## 5. Cross-cutting concerns

### 5.1 MQTT topic structure

Two namespaces:

**HA discovery** (`homeassistant/`): standard HA MQTT discovery topics. Discovery configs are retained. State and command topics are entity-specific.

**Browser/card** (`nibe/browser/`): bridge-specific topics consumed by the Entity Manager card. All retained except command topics. Key topics:

| Topic | Content |
|---|---|
| `nibe/browser/all_metadata` | Gzip-compressed JSON of all point metadata |
| `nibe/browser/enabled_state` | Current enabled/disabled state per point |
| `nibe/browser/dynamic_point_map` | Serialised DynamicPointMap |
| `nibe/browser/changelog/history` | Persistent changelog of dynamic point changes |
| `nibe/browser/snapshots` | Current snapshot list |
| `nibe/browser/bridge/status` | `online` / `offline` |
| `nibe/browser/bridge/alert` | Active alert message or empty |

### 5.2 Pending write guard

When a write command arrives on a command topic:
1. The point is marked pending in `EntityManager`
2. The write is dispatched to the write executor
3. State publishes for that point are suppressed until the executor completes
4. On success: confirmed state is published
5. On failure: the pending mark is cleared and the previous state is republished

`optimistic: false` in the discovery config ensures HA does not reflect the command payload in the UI before the bridge confirms.

### 5.3 Startup sequence

1. Parse configuration and build infrastructure
2. Connect to MQTT broker
3. Call `discover_points()` — fetches all point metadata from the API
4. Call `scan_mqtt_discovery()` — reads retained discovery configs to recover previously enabled entities
5. Call `restore_from_mqtt()` — reads retained state topics to recover last-known values
6. Apply initial mode (or restore from `/data/applied_mode`)
7. Start registry watcher thread
8. Start Lovelace provisioning thread
9. Publish snapshots
10. Enter poll loop

Steps 3–5 happen before the poll loop starts, so HA always has a consistent entity set on app restart.

### 5.4 Alarm polling

Alarms use a separate fast poll (`_ALARM_POLL_INTERVAL = 10s`) independent of the main bulk poll interval. This is fixed and not user-configurable — alarm latency is safety-relevant and should not be degraded by a slow poll interval choice.

---

## 6. Test suite

~4,337 tests (plus 19 Hypothesis subtests) across 26 files, at 100% line coverage. Philosophy: correctness over coverage metrics — the suite exists to make refactoring safe, not to hit a percentage target, but a full mutation-testing pass (mutmut) across every module confirmed the coverage is substantive rather than incidental.

For setup instructions and how to run the suite locally, see [CONTRIBUTING.md](CONTRIBUTING.md).

**Structure:**
- `conftest.py` — shared fixtures, Hypothesis strategies, profile registration
- One test file per source module for most modules — see the ownership table in [CONTRIBUTING.md](CONTRIBUTING.md#test-file-ownership)
- `test_entity_manager.py` plus 8 subsystem-split files (`test_entity_manager_snapshots.py`, `_changelog.py`, `_dynamic.py`, `_polling.py`, `_commands.py`, `_lifecycle.py`, `_state.py`, `_discovery.py`) cover `nibe_entity_manager.py`, which outgrew a single test file
- **Real-infrastructure integration suites** — every other file above proves the code calls its collaborators (an MQTT client, `urlopen`, a WebSocket) correctly using mocks; these instead run the real thing (a real mosquitto broker, a real `http.server`, a hand-built real WebSocket server, real filesystem operations) to prove the actual wire/OS-level behaviour, not just the mocked contract:
  - `test_api_integration.py` — `NibeApiClient` against a real HTTP(S) server (retry/error handling, TLS verification, real Authorization-header checking, chunked transfer-encoding)
  - `test_mqtt_broker_integration.py` — discovery publish/cleanup, unclean disconnect + resubscribe, broker restart, outbound queue backpressure, broker auth rejection, corrupted retained configs, oversized packets — against a real mosquitto broker. Skipped unless `NIBE_MQTT_TEST_HOST` is set (see `dev/mosquitto.sh`)
  - `test_ha_supervisor_integration.py` — `notify_ha`/`dismiss_ha` and `HAEntityRegistryWatcher` against a real HTTP + hand-built WebSocket stub Supervisor (auth rejection, dropped connections, ping/pong keepalive, reconnect racing a debounced refresh)
  - `test_filesystem_integration.py` — `wanted_points.json`, `dynamic_point_map.json`, and `menu_structure.yaml` recovery against real file corruption, permission-denied, and disk-full conditions
  - `test_end_to_end_startup.py` — a full `_build_infrastructure()` + `_run_startup_sequence()` + `_shutdown()` run against real stubs for all three external interfaces (Nibe API, MQTT broker, HA Supervisor) at once. Skipped unless `NIBE_MQTT_TEST_HOST` is set; must not run concurrently with `test_mqtt_broker_integration.py` against the same broker (see that file's own docstring)

**Testing approaches used:**
- `unittest.TestCase` + `MagicMock` for unit tests
- Hypothesis property-based tests with `@example` pins grounded in real firmware observations
- `RuleBasedStateMachine` state machines for complex subsystems: `EntityManagerMachine`, `LRUCacheMachine`, `DynamicPointMapMachine`, `ValueCacheMachine`
- pytest-xdist parallel execution (nightly, `-n auto`, 4 cores)
- pytest-randomly for test order randomisation

**Hypothesis profiles:**

| Profile | max_examples | Notes |
|---|---|---|
| `ci` | 20 | Fast, used in normal `pytest` runs |
| `thorough` | 500 | Manual deep runs |
| `nightly` | 500 + stateful_step_count=50 | Full nightly suite |

All profiles use `database=None` — required to prevent `FlakyStrategyDefinition` errors from non-deterministic Unicode surrogate hashing on Python 3.12+.

**Critical constraints:**
- `@freeze_time` decorator is **forbidden** in `test_entity_manager.py`. It causes xdist worker contamination of iterator-based `time.time` mocks. Use `patch('nibe_entity_manager.time.time', return_value=...)` instead.
- `TestRestoreSnapshot.setUp` patches `_read_applied_mode_from_file` to return `'essential'` — prevents xdist cross-worker `/data/applied_mode` file contamination.
- All test paths must patch `notify_ha` and `dismiss_ha` — live calls during test runs create persistent HA notifications.

**Mutation testing status:** all modules (`nibe_mqtt_publisher.py`, `nibe_discovery_config.py`, `nibe_entity_detection.py`, `nibe_dynamic_map.py`, `nibe_api.py`, `nibe_entity_manager.py`, `nibe_ha_integration.py`, `nibe_lovelace.py`, `nibe_caching.py`, `nibe_test_runner.py`, `nibe_utils.py`, `generate_nibe_mqtt.py`, `nibe_connectivity_check.py`) are at the structural ceiling — every survivor from the most recent run of each was individually diffed and either given an exact-value/exact-text test or confirmed as a genuine equivalent mutant (log format strings inside multi-line calls, codec-name case variants, dead-code fallback defaults, branches converging on identical output). `_fetch_bulk_data`/`_publish_dynamic_changes` in `nibe_entity_manager.py` are exempt from *speculative source refactoring* (§7) but their *tests* were reviewed and strengthened like any other function's.

**Process note on empirical verification:** mutation-testing empirical verification (apply a mutant for real, run the test suite, check pass/fail) must be run in the **foreground**, in small batches — never as an unattended background batch script. This project's per-module workflow shares one `mutants/` sandbox directory and one `pyproject.toml`, both overwritten whenever work switches to a different module; a background verification loop can silently have its sandbox swapped out from under it by a concurrent or closely-sequenced operation, producing verdicts that look complete and consistent but are wrong.

**mutmut reliability caveat:** mutmut 3.7.0 (this project's pinned version) calls `pytest.main()` **in-process** — once for its own baseline coverage-collection stats and again per mutant tested. Calling `pytest.main()` more than once in the same interpreter is explicitly unsupported by pytest, and has been observed to corrupt `tmp_path_factory`'s internal cleanup and crash the worker, producing false `survived`/`no tests` verdicts even on an already-fully-closed module. **No bulk `mutmut run`/`mutmut results` output — even from a single-module isolated run — may be trusted without per-mutant empirical verification**: apply the diff from `mutmut show <id>` by hand and run the real test file with a plain `pytest` invocation, not through mutmut. `mutmut show <id>` itself remains reliable (only applies a patch and prints it, no test execution). `./run-mutmut-single.sh <module> <test_file>` scopes a run to one module + its test file for faster, lower-blast-radius investigation, but does not fix the underlying unreliability.

Note: `run-mutmut.sh` regenerates the `mutants/` sandbox from scratch on every invocation, so a run's exact kill-rate/survivor numbers aren't retained once a later run overwrites the sandbox. Record `mutmut export-cicd-stats` output somewhere durable immediately after a run finishes if the exact figures need to survive past the session that produced them.

Pragma syntax: `# pragma: no mutate` (space required) on the **closing `)` line** of the statement.

---

## 6a. Backlog — planned, not yet started

Nothing currently pending. Recently-closed items are folded into §4 (module reference) where the knowledge is permanently relevant.

---

## 7. Parked work

These items were considered and deliberately not pursued. Recorded here to avoid relitigating the decisions.

| Item | Reason parked |
|---|---|
| Speculative refactoring of `_fetch_bulk_data` (~297 lines) | Complexity is inherent to the algorithm; fully covered; no current bug |
| Speculative refactoring of `_publish_dynamic_changes` (~215 lines) | Same rationale |
| Moving EntityManager constructor params to `__init__` | `device_info` requires an API response — awkward at construction time; low benefit for single-developer project |
| Contract emulator / end-to-end testing | Requires hardware emulation of the Nibe REST API; high effort |
| Spot price registers 26817–26840 | Modbus TCP write path; undocumented format |
| Zone temperature setpoints 32342–32380 | Zones 2–40 report value 0; unclear if real hardware |
| Modbus TCP sensor injection | Safety concern — writing arbitrary Modbus registers |
| Aid Mode re-test | Requires re-enabling backup heater in installer settings first |
| WebSocket `ws.close()` testability | Paho internal; not worth mocking |
| ThreadPoolExecutor worker testability | Threading internals; not worth mocking |

---

## 8. Hardware notes

**Target hardware:** Hardkernel ODROID-M1 (aarch64), running Home Assistant OS. AppArmor profile (`apparmor.txt`) is present but has no effect on aarch64 — the ODROID-M1 kernel does not include AppArmor support. The profile is effective on amd64 installations.

**Controller:** SMO S40 with S2125 heat pump. The S2125 has reversible cooling built in natively — no separate cooling accessory required.

**Minimum firmware:** 4.5.7. Tested on 4.12.6.

**THS-10 room sensor overrides** required because firmware reports non-standard units or wrong entity types:
- Point 50827 (humidity): `UNIT_OVERRIDES` with `"%"` — firmware reports `%RH` which HA does not accept
- Points 5110, 5214, 32824: `ENTITY_TYPE_OVERRIDES` as `'switch'`

**Other confirmed overrides:**
- Points 8982, 3754: `'switch'` — firmware reports `max=0`, auto-detection fails
- Point 22077 (AUX from Modbus): `'binary_sensor'` — s16 + isWritable=True auto-detects wrong
- Point 1948 (Holiday function): `'sensor'` — managed via `/aidmode` endpoint, not PATCH
