# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

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
