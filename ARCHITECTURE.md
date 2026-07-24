# Architecture — Nibe S-Series MQTT Bridge

Developer reference. Not user documentation — see [DOCS.md](nibe_s_series/DOCS.md) for installation and configuration.

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

---

## 4. Module reference

### 4.1 `generate_nibe_mqtt.py` — entrypoint

The orchestration layer. Owns configuration parsing, startup sequencing, the poll loop, and clean shutdown. Contains no business logic.

`main()` (~70 lines) delegates to four named functions:

- `_build_infrastructure(cfg)` — creates the API client and MQTT client, connects to the broker, and returns a `set_entity_manager` closure used to wire the entity manager into MQTT `on_connect` after it is constructed
- `_run_startup_sequence(...)` — assembles all subsystems, runs discovery and MQTT scan, applies the initial mode, starts threads, publishes snapshots
- `_poll_loop(em, pub, initial_mode)` — runs until `KeyboardInterrupt`; implements exponential backoff (5s × N, capped at 60s) and a bridge alert after 5 consecutive failures
- `_shutdown(...)` — drains executors with a 35-second hard timeout, publishes offline status, optionally clears retained MQTT topics (`NIBE_REMOVE_FRONTEND=1`)

**Configuration priority** (highest → lowest): CLI flags → `/data/options.json` → `/config/secrets.yaml` or `/homeassistant/secrets.yaml` → defaults.

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

**`clean_string()`** strips `U+00AD` soft-hyphens that the firmware embeds in register titles. These are invisible in most editors but corrupt entity IDs if not removed.

**Mode sets** (`ESSENTIAL_POINTS`, `MONITORING_POINTS`, etc.) are `frozenset[int]` literals. `all` uses `None` as sentinel, replaced at runtime with the full discovered point list. `none` is an empty frozenset.

**What this module does not do:** no I/O, no state, no side effects of any kind.

---

### 4.4 `nibe_entity_manager.py` — EntityManager

The largest module (~3,250 lines) and the core of the bridge. Owns the full lifecycle of every data point as a HA entity.

**Point registry:** all discovered points are indexed by `variableId` in a dict. The registry is populated once at startup via `discover_points()` and is read-only thereafter (dynamic points are handled separately).

**MQTT-first state:** retained discovery configs in the broker are the single source of truth for which entities are enabled. On restart, `scan_mqtt_discovery()` reads these back rather than keeping a separate state file. This means the bridge survives restarts without losing user customisations.

**Value cache (`ValueCache`):** suppresses redundant MQTT state publishes via two guards:
- Change threshold: floating-point values within a small epsilon of the previous published value are not republished
- Minimum interval: a value that has not changed is still republished after a configurable interval to keep HA's "last changed" timestamp meaningful

**LRU cache:** entity type classifications and point string representations are cached in an `LRUCache(max_size=2000)` to avoid recomputing on every poll cycle.

**Pending write guard:** when a write command is dispatched, the affected point is marked "pending". State publishes for that point are suppressed until the API confirms the write or the guard times out. This prevents the HA UI from flipping back to the old value while the write is in flight. `optimistic: false` is set on all writable discovery configs for the same reason.

**Snapshot save/restore:** up to 10 named snapshots of the enabled entity set are persisted to `/data/snapshots.json`. Restore is blocked in `menus` and `all` mode (too many entities to restore safely). Flush mode replaces the entire enabled set; merge mode adds to it.

**Two functions that must not be refactored speculatively:**
- `_fetch_bulk_data()` (~297 lines): the complexity is inherent. It simultaneously manages the string cache, bulk data mutation, new-point classification, baseline tracking, and disappeared-set computation. Extracting sub-functions is feasible but high-risk for a fully-covered function with no current bug.
- `_publish_dynamic_changes()` (~215 lines): same rationale. Handles the full causal chain from point appearance/disappearance through DynamicPointMap update, HA notification, changelog entry, and dashboard regen scheduling.

**What this module does not do:** no raw HTTP, no MQTT topic string construction, no HA registry watching, no notification sending.

The MQTT protocol between the bridge and the Entity Manager card is documented in [`docs/card-api.md`](docs/card-api.md).

---

### 4.5 `nibe_mqtt_publisher.py` — MqttDiscoveryPublisher

Single source of truth for all MQTT topic strings and discovery config payloads. Builds and publishes HA MQTT discovery configs for every entity type the bridge supports.

**Topic ownership:** all fixed topic strings are defined as class attributes on `MgmtTopic` and `BrowserTopic` enums. No topic string is constructed outside this module.

**Discovery config structure:** each entity type (sensor, binary_sensor, switch, select, number, button) has a dedicated builder that populates the mandatory and optional HA discovery fields. The builders use `optimistic: false` on all writable types to prevent HA from optimistically reflecting the command before the bridge confirms the write.

**Debug-only entities:** `Flush Dynamic Map`, `Run Test Suite`, and `Test Suite Result` are only published when `debug_mode=True`. When `debug_mode=False`, empty retained payloads are sent to those discovery topics so HA removes the entities. A full HA restart is required for the entity registry to reflect the removal.

**What this module does not do:** no HTTP, no entity lifecycle tracking, no threading.

For the full topic list and JSON schemas for every topic this module publishes, see [`docs/card-api.md`](docs/card-api.md).

---

### 4.6 `nibe_dynamic_map.py` — DynamicPointMap

A causal table recording which writable switch/select points cause dynamic points to appear or disappear in the firmware's bulk fetch response.

**Why this exists:** some Nibe operating modes expose extra registers only while active (e.g. manual-mode setpoints appear when the heating curve is switched to manual). Without the map, the bridge can only detect these by comparing two consecutive bulk fetches — slow and unreliable. With the map, a write to a known controlling point immediately triggers a targeted probe of its known dynamic points via `fetch_point()` rather than waiting for the next bulk cycle.

**Causal learning:** the map self-populates. On first observation of a write to a switch/select point, the bridge records the outcome (which dynamic points appeared or disappeared) as a `DynamicPointEntry`. Subsequent writes to the same point bypass the detection cycle entirely and apply the known outcome immediately.

**Persistence:** the map is serialised to JSON and published to a retained MQTT topic (`nibe/browser/dynamic_point_map`) so it survives restarts. The module has no I/O of its own — all persistence is delegated to `EntityManager._persist_dynamic_map()`.

**What this module does not do:** no I/O, no MQTT, no HTTP. Pure data structure with JSON serialisation.

---

### 4.7 `nibe_ha_integration.py` — HA integration layer

Everything that talks to HA itself rather than to the Nibe device or the MQTT broker.

**`notify_ha()` / `dismiss_ha()`:** create and clear HA persistent notifications via the Supervisor REST API (`http://supervisor/core/api/services/notify`). Notification links use absolute URLs constructed by `_get_ha_base_url()` — relative `/local/` paths are intercepted by the HA frontend router and do not navigate correctly from notification cards.

**`_get_ha_base_url()`:** fetches `internal_url` / `external_url` from `GET http://supervisor/core/api/config`. Result is cached in a module-level global after first fetch. Returns `''` on failure so callers always get a string.

**`HAEntityRegistryWatcher`:** a long-lived WebSocket subscriber to `ws://supervisor/core/websocket`. Subscribes to `entity_registry_updated` events and maintains a local cache of `unique_id → entity_id` mappings. This replaces the previously required companion HA automation. The watcher handles the known HA behaviour where MQTT entity create events omit `unique_id` from the event payload — on such events it triggers an asynchronous full registry refresh.

**Management command handlers:** `ManagementCommandHandler` subscribes to management MQTT topics published by the card and HA buttons. Duplicate button presses while a test run is in flight are dropped silently via a `threading.Event` guard. Exit codes: `0` = passed, `-1` = timed out, `-2` = launch error, other = failed.

**What this module does not do:** no Nibe API calls, no discovery config publishing, no entity lifecycle management.

---

### 4.8 `nibe_lovelace.py` — Lovelace provisioning

All interaction with the HA frontend. Creates and maintains two dashboards and the companion card resource registration.

**Nibe Bridge dashboard:** created once on first start. Never overwritten — if the user deletes it, it is recreated on the next restart. Contains the Entity Manager card and management controls.

**Nibe Menus dashboard:** rebuilt from scratch on every restart (and on-demand via "Regenerate Dashboard"). Mirrors the full SMO S40 installer menu structure from `menu_structure.yaml` (163 menus, ~350 settings). Dynamic points are injected below their controlling switch when active and removed when inactive.

**Dashboard creation guard:** the check for whether the dashboard already exists requires a successful `lovelace/dashboards` list call. A failed list call is distinguished from "zero dashboards exist" — a failed call does not proceed to a creation attempt that would always fail.

**Regen debounce:** menu dashboard regeneration is debounced — rapid enable/disable operations queue a single regen rather than triggering one per entity change. The debounce is wired into `EntityManager` via a callback registered by `schedule_menu_dashboard_regen()`.

**Retry logic:** dashboard regen retries up to 3 times at 3-second intervals when active dynamic point entity IDs are not yet in the HA entity registry, handling the race between MQTT discovery processing and dashboard build.

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

Steps 3–5 happen before the poll loop starts, so HA always has a consistent entity set on add-on restart.

### 5.4 Alarm polling

Alarms use a separate fast poll (`_ALARM_POLL_INTERVAL = 10s`) independent of the main bulk poll interval. This is fixed and not user-configurable — alarm latency is safety-relevant and should not be degraded by a slow poll interval choice.

---

## 6. Test suite

~2,690 tests across 10 files. Philosophy: correctness over coverage metrics. The suite exists to make refactoring safe, not to hit a percentage target.

For setup instructions and how to run the suite locally, see [CONTRIBUTING.md](CONTRIBUTING.md).

**Structure:**
- `conftest.py` — shared fixtures, Hypothesis strategies, profile registration
- One test file per source module — see file layout in README

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

**Mutation testing status:**
- Phase 1 (`nibe_mqtt_publisher.py`): ceiling reached (~77% kill rate, 1,978 mutants)
- Phase 2 (`nibe_entity_detection.py`, `nibe_dynamic_map.py`, `nibe_api.py`): ceiling reached (~68% kill rate, 1,196 mutants)
- Phase 3 (`nibe_entity_manager.py`): parked — estimated 50–80 hours runtime on ODROID-M1
- Phase 4 (`generate_nibe_mqtt.py`): parked — threading-heavy survivors unresolvable
- Never mutmut'd: `nibe_ha_integration.py`, `nibe_lovelace.py`, `nibe_utils.py`

Survivors in Phases 1 and 2 are at the structural ceiling: log format string mutations inside multi-line calls (pragmas on the closing `)` do not suppress inner line mutations) and genuine semantic equivalents.

Pragma syntax: `# pragma: no mutate` (space required) on the **closing `)` line** of the statement.

---

## 7. Parked work

These items were considered and deliberately not pursued. Recorded here to avoid relitigating the decisions.

| Item | Reason parked |
|---|---|
| Speculative refactoring of `_fetch_bulk_data` (~297 lines) | Complexity is inherent to the algorithm; fully covered; no current bug |
| Speculative refactoring of `_publish_dynamic_changes` (~215 lines) | Same rationale |
| Moving EntityManager constructor params to `__init__` | `device_info` requires an API response — awkward at construction time; low benefit for single-developer project |
| Mutation testing phases 3 and 4 | Runtime cost (50–80h) exceeds value; already at 100% line coverage |
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
