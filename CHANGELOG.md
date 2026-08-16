# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

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
