# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added
- **Entity name localization** — a new `language` option translates entity
  titles/descriptions via the Nibe REST API's own `Accept-Language`
  support (confirmed against a live SMO S40: `nl`/`de`/`sv` return
  correctly translated text). `auto` (default) detects Home Assistant's
  own configured language via the Supervisor API; any other value
  overrides auto-detection. The dropdown mirrors the controller's own
  hand-verified "Language" select entity (`VALUE_MAPPINGS[3745]`).
  Does not translate the "Nibe Menus" Lovelace dashboard, which stays
  static English content by design. An unsupported language, an
  unrecognised one, or an individual point missing from NIBE's own
  translation catalog all fail safely — no error, silent fallback to
  English, matching the API's own default behaviour.
- **`mode_switch_behavior` option** (`replace`/`merge`, default
  `replace`) — controls what happens to already-enabled entities when
  switching `mode`. `replace` keeps the existing behaviour: entities not
  in the newly selected mode's set are disabled, including anything
  enabled manually via the Entity Manager card. `merge` only ever adds
  the new mode's points; nothing already enabled is ever disabled by a
  mode change.
- 5 new `translations/*.yaml` files (French, Spanish, Italian, Czech,
  Finnish) covering the app's own Configuration UI, added for NIBE's
  stated strong-presence markets (Sweden, Norway, Finland, Denmark,
  Germany, France, UK, Netherlands, Poland, Czech Republic, Italy,
  Spain) alongside the 7 that already existed.
- A test (`TestConfigTranslationsParity`) enforcing that every
  `config.yaml` option has a matching, non-empty translation entry in
  every `translations/*.yaml` file, and flagging stale entries for
  removed options.

### Fixed
- Point `1760` ("Operating mode internal add. heat") was misclassified
  as a `binary_sensor` — it's actually a 4-state integer (0–3). The
  Nibe REST API reports `minValue: 0, maxValue: 0` for this point, which
  is indistinguishable from the metadata shape of ~125 genuinely binary
  points on the same firmware (confirmed against a real 1158-point API
  dump), ruling out a general heuristic fix; resolved with a targeted
  `_BINARY_SENSOR_EXCLUSIONS` entry, the same approach already used for
  other points sharing this ambiguity.
- `clean_string()` was not idempotent: mojibake-character removal ran
  *after* quote-stripping, so a string like `0"Â` could leave a stray
  trailing `"` that only a second call would strip. Reordered so
  mojibake/soft-hyphen removal happens before quote-stripping.
- A `select` entity's write path could silently drop a legitimate
  command if its value-mapping labels were built from live API
  description text in a different state than what was used to publish
  the entity's options to HA (e.g. after a restart with a different
  `language`). It now accepts a raw integer payload as a fallback when
  it's one of the mapping's own known keys, instead of only matching by
  label text.
- `DOCS.md` incorrectly stated that `secrets.yaml` credentials take
  priority over the app's configuration UI — it's actually the lowest-
  priority source and only fills in fields left blank in the UI.
- The **Test API Connection** diagnostic could report the controller as
  unreachable even when the bridge's real polling connection worked fine,
  because the diagnostic's `curl` call didn't widen OpenSSL's default TLS
  security level the way the real connection does to tolerate the
  controller's old embedded TLS stack. The compatibility cipher string is
  now a shared constant (`TLS_COMPAT_CIPHERS` in `nibe_utils.py`) used by
  both, so the diagnostic mirrors the real connection's TLS behaviour.
- The connectivity check's summary logic conflated "curl never reached
  the host" with "curl reached the host but got an error status" (e.g.
  HTTP 500), and a 401/403 response discarded any concurrent ping
  failure from the summary. Now distinguishes "reached the host" from
  "genuine network outage," and reports combined failure modes (e.g.
  blocked ICMP *and* stale credentials) instead of only the first one
  found.
- Static MQTT entity attributes (description, default value, Modbus
  register, etc.) were republished unconditionally on every discovery
  call with no dedup, while the discovery config itself was already
  deduped by a content hash — meaning an attributes-only firmware change
  had no dedicated mechanism ensuring it got republished, and unchanged
  attributes were rewritten to the broker every poll for no reason. Added
  a separate attributes-content hash so attributes are republished only
  when they actually change, independent of the discovery config hash.
- The Lovelace menu dashboard treated a real firmware point with
  `variableId`/`point_id` of `0` identically to the schema's "no point"
  sentinel (`point_id: null`), because the code used a truthy check
  (`if point_id:`) rather than an explicit `is not None` check — silently
  dropping any such point from menus, defaults, and dynamic-point lookups.
- Lovelace dashboard creation could permanently skip creating the
  dashboard on a fresh install if the initial `lovelace/dashboards/list`
  WebSocket call itself failed (e.g. a dead socket) — an empty result
  from a failed call was indistinguishable from "no dashboard exists
  yet," so it proceeded as if nothing needed creating. Now checks the
  call's own success flag and retries on the next restart instead of
  writing a skip flag on a failed list.
- The nightly test-runner subprocess can leave its HTML report truncated
  mid-multibyte-UTF-8 sequence when killed (hard timeout or manual
  abort), which raised `UnicodeDecodeError` when post-processing the
  report — uncaught, this overwrote the carefully-tracked aborted/
  timed-out status with a generic error state. Now caught alongside the
  existing `OSError` handling.

### Changed
- `build_select_config()` no longer takes an unused `metadata` parameter,
  tightening the pure-config-builder contract now that entity options are
  resolved from `point_id`/`description` alone.

---

## [1.0.5] — 2026-08-18

### Added
- **Test API Connection** debug button and **Connectivity Check Result**
  sensor — runs an independent `ping` + `curl` diagnostic against the
  configured Nibe controller, using the bridge's real configured TLS
  verification mode and credentials, so it can distinguish a network
  problem, a TLS/CA problem, and a rejected-credentials problem from each
  other without needing SSH/terminal access to the Home Assistant host.
  Deliberately shares no code with `NibeApiClient` so the result is
  independent of any bug in the bridge's own HTTP client.
- The "API Unreachable" notification (both at startup and after repeated
  failed polls) now includes the actual last error reason from the API
  client (e.g. "timed out waiting for a response" instead of a blank
  message for exceptions with no message text) and points the user at the
  new Test API Connection button for further diagnosis.

### Fixed
- The controller's HA device identity (`device_id`) was derived fresh from
  the API response at every startup, falling back to a generic default
  whenever the device happened to be transiently unreachable at that exact
  moment. Since the Management device is published unconditionally at
  every startup regardless of discovery success, a startup that hit this
  fallback created a *new* HA device under a different identity, leaving
  the previous one behind as an orphaned, empty duplicate with the same
  display name. The real, serial-derived device_id is now persisted and
  reused on any startup where the device is unreachable, instead of
  falling back to the generic default.
- A `select` entity's live state could silently diverge from its discovery
  config's option list (e.g. showing the raw firmware description text
  like `"price"` instead of the curated override `"Price per kWh"`) if the
  point's metadata happened to be incomplete on the specific poll that
  first populated its cached value mapping — the mapping is now looked up
  by point ID across the manual override table unconditionally, instead of
  only when the register type could be resolved from that poll's metadata.
- Several concurrency/locking gaps found via targeted audit: a Lovelace
  provisioning-thread iteration over live, unlocked dicts also mutated by
  the poll thread; `last_bulk_fetch` written outside the lock that
  protects it elsewhere; a publisher warning-dedup set with a check-then-add
  race across threads; and `_mgmt_subscriptions` read/appended without a
  lock during MQTT reconnect replay.
- The nightly test-runner subprocess launch could fail with an
  unhelpful, un-diagnosable "permission denied" — the AppArmor profile
  had no rule granting traversal into `/` itself (only subpaths), which
  the subprocess's `cwd='/'` needs; the `TimeoutExpired` handler also now
  captures and logs the subprocess's real output and reports actual
  elapsed time, instead of a generic message assuming the 4-hour hard
  limit was reached.
- A stale test subprocess from a previous run (e.g. after an abnormal
  restart) is now killed before starting a new one, rather than
  potentially running concurrently with it.
- The nightly test suite could fire real Home Assistant persistent
  notifications from fabricated test fixture data — two tests exercised
  the real, unmocked `notify_ha`/`dismiss_ha` functions (which make a
  genuine HTTP call to the Supervisor API, gated only by `SUPERVISOR_TOKEN`
  being present, which the test subprocess inherits from the live add-on
  process). Root cause of a real, reproducible false "Critical" alarm
  notification a user saw with no matching alarm on the physical
  controller, and a spurious dismiss-notification call on every
  successful nightly run.
- `debug_mode` and `log_level: debug` both independently controlled
  whether debug-only entities (Run Test Suite, Flush Dynamic Map, etc.)
  were shown — now `debug_mode` is the sole control; `log_level` only
  affects logging verbosity.
- `debug_mode`, `remove_frontend`, and `mqtt_tls` were declared as
  optional (`bool?`) in `config.yaml`'s schema despite each having a real
  default, unlike every other option with a default — normalized to
  required `bool`, matching the project's own established convention.
- The License badge/link in `README.md` used a relative path, which
  doesn't resolve inside Home Assistant's own add-on documentation
  viewer (it only renders the single file it's given, not the rest of
  the repo) — now points at the absolute GitHub URL, matching the
  README's other doc links.
- The self-signed-TLS fallback for the Nibe API connection lowered the
  minimum TLS version and permitted weaker ciphers beyond what's needed
  just to accept a self-signed certificate — removed; the fallback still
  accepts an unverified certificate (its intentional purpose) but now
  uses Python's modern secure defaults otherwise.
- The generated nightly test report link had no cache-busting, so
  browsers could keep serving a stale cached copy after a new run —
  added a `?v=<timestamp>` query parameter.

### Changed
- Several `except Exception` blocks across `app/` narrowed to the actual
  bounded set of exceptions the wrapped code can raise, improving
  diagnosability of unexpected failures — reverted in the one case
  (`on_dynamic_map_message`) where the narrower set turned out to be
  incomplete, since that handler wraps a call into another object's
  method whose full exception contract shouldn't be assumed.
- Several config.yaml options (`api_failure_threshold`,
  `changelog_retention_days`, `mode`, `log_level`) are now validated or
  bounds-clamped in Python as well as HA's schema, closing a bypass via
  hand-edited `options.json` or the dev-only `NIBE_MODE`/`NIBE_LOG_LEVEL`
  environment variables — an invalid `mode` previously silently disabled
  every enabled entity with no distinguishing warning.

---

## [1.0.4] — 2026-08-16

### Fixed
- MQTT discovery configs for every enabled entity were being republished
  unconditionally on every restart, regardless of whether anything
  actually changed — the dedup cache that's supposed to prevent this
  lives only in memory on the MQTT publisher, and that publisher is
  rebuilt fresh every restart. The retained-config scan performed at
  startup now seeds the cache from what it actually finds on the broker,
  so a restart with no real changes now republishes little to nothing
  instead of every entity's config.
- `NIBE_LOG_LEVEL`/`NIBE_MODE` environment variables (documented for
  development/Docker use) were silently ignored whenever the bridge was
  invoked through its normal CLI argument parser, because the parser's
  own defaults made the CLI arguments always look "explicitly set,"
  which unconditionally overrides lower-priority sources. Only affected
  invocations that bypass `run.sh`'s own options.json-to-CLI passthrough
  (e.g. running the container directly); the packaged add-on's normal
  startup path was unaffected.
- A changelog "mark all read" action updated its internal bookkeeping
  before, rather than after, its MQTT publish calls — unlike the
  equivalent history-update path, which does this in the opposite order
  deliberately for crash-safety. A broker reconnect racing the publish
  could let a stale retained changelog message override the
  just-cleared unread state.
- A file-persistence path in the dynamic-point-map fallback used a
  default argument that was bound once at startup rather than resolved
  per call — invisible in normal operation, but inconsistent with how
  the same problem was already solved elsewhere in this codebase.

### Changed
- Deduplicated an HA-side notification identifier that was independently
  computed in two separate places — no behavior change, removes a latent
  risk of the two copies drifting out of sync in the future.

---

## [1.0.3] — 2026-08-15

### Fixed
- MQTT auto-discovery via the Supervisor Services API silently overrode an
  explicitly configured `mqtt_host` (e.g. a user's own external broker IP)
  whenever the official Mosquitto add-on was installed and registered —
  the startup log would show `core-mosquitto:1883` instead of the
  configured broker, with the connection then failing because that
  internal hostname isn't resolvable outside HA's own Docker network.
  Auto-discovery now only runs when `mqtt_host` is still at its default
  value; any other value means the user made an explicit choice and is no
  longer silently overridden.
- `resubscribe_all()` (running on paho's MQTT network thread after a
  broker reconnect) reassigned `value_cache` and `last_bulk_fetch` with no
  lock, racing against the poll loop thread's own reads/writes of the same
  two attributes. Both races were bounded and self-healing in practice,
  but relied on CPython implementation details rather than being correct
  by construction — both are now reassigned under `_em_lock`.
- The "Flush Dynamic Map" debug button mutated `dynamic_point_map` and
  persisted it to disk without holding `_em_lock`, while the poll thread
  and the write-executor thread both correctly serialize the same table
  via that lock — a genuine data-structure race, not just a stale read: a
  flush landing mid-mutation on either of those threads could corrupt
  `dynamic_point_map._table`. Now serialized the same way as the other two
  mutators, with a regression test proving real mutual exclusion.
- A logger-level leak in three `_build_logging` tests (`tests/test_generate.py`)
  left `logging.getLogger('nibe')` at `DEBUG`/`INFO` for the rest of the
  test session depending on execution order, which under one unlucky
  `pytest-randomly` seed turned a mocked-time deadline check in
  `_run_learning_detection` into a genuine CPU-bound infinite loop —
  the actual cause of an intermittent CI timeout. Each test now restores
  the logger's level in its `finally` block.
- The pytest subprocess behind the "Run Test Suite" debug button now
  launches with `start_new_session=True`, and a new `abort_test_suite()`
  kills its whole process group (not just the top-level PID) as the first
  step of the add-on's shutdown sequence — killing only the top-level
  process left `pytest -n auto`'s xdist workers running as orphans, still
  holding the output pipes open, so shutdown would hang waiting for pipe
  EOF that never came. An aborted run is now reported as a distinct
  `aborted` status with no HA notification, instead of misreporting the
  kill's exit code as a real test failure.
- `run_test_suite` now resolves the pytest interpreter via `shutil.which()`
  when `sys.executable` comes back empty (observed on the ODROID's
  Alpine/musl container), and runs on its own dedicated executor instead
  of sharing the 2-worker `mgmt_executor` pool, so a long test run can no
  longer be queued behind (or itself block) unrelated management commands.
- Fixed the success-path test-result summary: the raw pytest-html report
  line wasn't reliably stripped, skipped-test progress dots weren't
  recognised as noise, and the replacement report link was plain text
  instead of a real Markdown link, which silently broke its clickability.

### Added
- Dependabot config (`pip` + `github-actions`, grouped weekly updates) and
  a CI workflow that runs the full test suite on every push/PR to `main`.

[1.0.3]: https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/releases/tag/v1.0.3

---

## [1.0.2] — 2026-08-14

### Fixed
- The "Run Test Suite" debug feature used `subprocess.run()` to launch the
  bridge's own pytest suite, which blocks with no way to cancel it
  externally. Python's own `atexit` hook for `ThreadPoolExecutor` then
  blocked the whole add-on's process exit until that subprocess finished on
  its own — up to the full 25-30+ minute run — so rebuilding or stopping
  the add-on while a test run was in flight got the container SIGKILLed by
  Docker's stop grace period (exit code 137) instead of shutting down
  cleanly.
- The pytest subprocess now launches with `start_new_session=True`, and a
  new `abort_test_suite()` kills its *entire process group* (not just the
  top-level PID) as the first step of the add-on's shutdown sequence —
  killing only the top-level process left `pytest -n auto`'s xdist worker
  subprocesses running as orphans, still holding the output pipes open, so
  the shutdown sequence would hang waiting for pipe EOF that never came.
- An aborted run (killed because the add-on is shutting down) is now
  reported as a distinct `aborted` status with no HA notification, instead
  of misreporting the kill's exit code as a real test **FAILED** result.
- `run_test_suite` now resolves the pytest interpreter via `shutil.which()`
  when `sys.executable` comes back empty — observed on the ODROID's
  Alpine/musl container, where the bare `'python3'` fallback previously
  depended on the *subprocess's* own `PATH` resolution and failed with
  "no such file: python3".
- Fixed the success-path test-result summary: the raw pytest-html report
  line wasn't reliably stripped (the noise filter required an exact
  3-dash prefix; real output sometimes used a different dash count),
  skipped-test progress dots (`s`) weren't recognised as noise either, and
  the replacement report link was written as plain text instead of a real
  Markdown link, which silently broke its clickability. Added a
  right-click hint, since Home Assistant's frontend intercepts same-origin
  left-clicks for its own client-side router rather than opening the link.

### Changed
- `run_test_suite` now runs on its own dedicated single-worker executor
  instead of sharing the 2-worker `mgmt_executor` pool with every other
  management command, so a long test run can no longer be queued behind
  (or itself block) unrelated commands like force-poll or snapshot
  restore.

[1.0.2]: https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/releases/tag/v1.0.2

---

## [1.0.1] — 2026-08-14

### Fixed
- Lovelace card file was copied to `/config/www` and the AppArmor profile granted
  `/config/**` rw, but this add-on's `homeassistant_config` map actually mounts the
  HA config directory at `/homeassistant`, not `/config`. The copy silently
  "succeeded" against a path inside the container's own ephemeral filesystem, so
  the card file never reached the host's real `www` folder — Home Assistant would
  then report `Custom element doesn't exist: nibe-entity-manager-card` even though
  startup logs claimed the file was copied and the Lovelace resource was
  registered. `nibe_lovelace.py` and `apparmor.txt` now target `/homeassistant`
  to match the actual mount.

### Changed
- Extracted `ValueCache`/`LRUCache` out of `nibe_entity_manager.py` into a new
  `nibe_caching.py` module.
- Extracted MQTT discovery config-building out of `nibe_mqtt_publisher.py` into a
  new, pure `nibe_discovery_config.py` module.
- Extracted `_handle_run_tests` test-execution logic out of `nibe_ha_integration.py`
  into a new `nibe_test_runner.py` module.
- `nibe_ha_integration.py`'s `_handle_event` branches now take a lock-protected
  snapshot of `_unique_id_map` instead of reading the live dict.
- `nibe_lovelace.py`: extracted `_wait_for_registry_stable` from
  `_setup_menu_dashboard`; fixed a debounce timer race with a new
  `_regen_timer_lock`.
- Split the ~9,800-line `tests/test_entity_manager.py` (which had accumulated
  ~120 duplicate/shadowed test classes) into 9 files by subsystem: snapshots,
  changelog, dynamic points, polling, commands, lifecycle, state, and discovery.
- Added a GitHub Actions workflow (`.github/workflows/publish-image.yml`) for
  building and publishing multi-arch add-on images (not yet enabled by default —
  `config.yaml`'s `image:` key stays commented out until the workflow has been
  run at least once).
- Relicensed under `LICENSE.md` (replaces the previous `LICENSE` file).
- Removed the unused `net_bind_service` AppArmor capability.

[1.0.1]: https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/releases/tag/v1.0.1

---

## [1.0.0] — 2026-07-23

Initial public release.


[1.0.0]: https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/releases/tag/v1.0.0
