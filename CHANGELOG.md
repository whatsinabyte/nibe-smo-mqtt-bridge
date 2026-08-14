# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

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
