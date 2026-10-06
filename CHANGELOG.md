# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.3.0] — 2026-10-06

A broad bug-hunting release. Most of it is fixes, but some entities change
type, so read **Changed** before upgrading.

### Changed

- **Some entities get a new entity id.** A changed entity type means Home
  Assistant creates a new entity: repoint automations and dashboards that
  use the old id, and expect its history to restart.
  - Option settings become dropdowns (`select`): blocking actions ERS 3–8
    (7022, 7023, 23141–23144), operating mode (5482), charging method
    (4692) and internal additional heat stepping mode (4085).
  - 4969/4970 (block freq 1/2, EB101) and 8060 (defrost requested, EB101)
    become switches.
  - The spot price registers (26817–26840), 55749 (block new compressor)
    and 55884 (set point value power) become read-only sensors. DOCS.md
    lists them as not meant to be written; that used to rest on a firmware
    flag that didn't cover all of them.
  - 2701 (ACS status) is a sensor from the start, instead of a
    binary_sensor that changed into one at the first real value.
- **Writability follows the register type**, not the firmware's
  `isWritable` flag, which is wrong for 17 holding registers. "Reset alarm"
  (3478) and operating mode (4064), among others, could never be written.
- **Log levels mean something:** WARNING and ERROR are only used for
  something to look at; expected behaviour (an applied override, a
  firmware convention) logs at DEBUG or INFO.

### Fixed

- **Values and units**
  - Inverter version (14987) shows `61` instead of `0.0.61`.
  - "All sub units operating prio" (56150) shows Idle / Heating / Cooling
    instead of a bare number.
  - The minimum of the periodic hot water stop temperature (3702) is
    55 °C; the firmware declares 5.5 °C.
  - Units added for registers the firmware reports without one (bar, °C,
    %, h, min, s, W, DM).
  - Switches and buttons that declare a 0–0 range (Activate forced
    control, Away mode, Reset alarm, …) can be switched on again.
  - A select reporting a value that is none of its options shows
    unavailable instead of keeping a stale option.
- **Entities that disappeared or came back wrong**
  - A sensor briefly missing from a poll right after a write is no longer
    deleted; it gets the same 5-minute grace as anywhere else.
  - An enabled point missing at startup is kept for 5 minutes instead of
    staying unavailable forever.
  - A wanted point that returns after an absence is re-enabled again.
  - Dynamic points of a dynamic select are no longer removed at startup,
    and a point shown by several of a select's options is kept.
  - Gone dynamic points no longer leave ghost entities after a restart.
  - A reclassified binary sensor stays a sensor across restarts.
  - Entities keep their attributes after a type change.
  - After a broker loses its retained messages, the bridge republishes
    everything instead of losing all entities.
- **Home Assistant integration**
  - A dynamic entity disabled in HA, also while the add-on was stopped or
    the controller was down, is really re-enabled; no false "re-enabled"
    notification.
  - An entity disabled in HA while the add-on was stopped is shown as
    disabled in the card.
  - The active alarms notification updates when the set of alarms
    changes; alarm and "API unreachable" notifications left from before a
    restart are cleared.
  - Aid/smart mode changed on the controller reaches HA within 5 minutes.
- **Dashboards and the Entity Manager card**
  - The Nibe Bridge dashboard is recreated when deleted, and gets its card
    if the first save failed.
  - Nibe Menus: dynamic points of dynamic points are shown, and so are
    points below a controller that isn't enabled in HA; renamed entities
    no longer leave "not available" rows; empty "0 – 0" ranges are hidden.
  - Uninstalling removes the Nibe Menus dashboard and the test report too.
  - Card: search no longer bypasses the filters (so "Select All" can't
    pick hidden entities); column sorting, the mobile filter toggle and
    Enable/Disable no longer fire twice; the card survives Lovelace
    re-applying its config; the point list and changelog stay current
    after a bridge restart.
- **Snapshots and modes**
  - Saving or restoring a snapshot reports its outcome in the card, so a
    refusal (the 10-snapshot limit) no longer looks like success.
  - Snapshots record the right mode after a reinstall, and the restore
    guard in menus/all mode works again.
  - A flush restore or replace-mode switch is no longer undone by points
    it excluded.
- **Configuration and connection**
  - Nibe credentials entered in the add-on UI take precedence over
    `secrets.yaml`, as documented.
  - A `# comment` after a value in `secrets.yaml` is no longer read as
    part of it.
  - `language: auto` handles regional variants (`en-GB`) and Norwegian
    (`nb`/`nn`).
  - Writes and management actions wait for the poll instead of sending
    requests to the controller at the same time.
  - The connectivity check no longer puts the controller credentials on
    the process list.
  - The built-in test suite no longer fails falsely, and a failure
    notification names a test that broke in setup.

### Added

- DOCS.md: external sensor readings (menu 7.5.9.2), Mosquitto credential
  precedence, and why a number or select can show unavailable.
- [docs/known-firmware-quirks.md](docs/known-firmware-quirks.md): new
  entries, including `isWritable`, version encodings, ranges and units.

### Internal

- The end-to-end suite runs on parallel stacks (about 14 instead of 26+
  minutes) with many new scenarios; property tests now always sample
  every specially treated point.

## [1.2.6] — 2026-10-02

### Fixed

- "Current status" (2022) now follows the configured `language` instead of
  always showing English
  ([#101](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/issues/101)).

## [1.2.5] — 2026-09-26

### Fixed

- An unconfigured zone temperature no longer makes Home Assistant log
  "Invalid value" after every restart: the stale value kept on the MQTT
  broker is now cleared when the entity is marked unavailable.

## [1.2.4] — 2026-09-24

### Fixed

- A rejected write (HTTP 403) now logs the controller's own error text
  instead of always blaming a wrong device id
  ([#95](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/issues/95)).

### Added

- DOCS.md warns that the REST API user (menu 7.5.15) must not be
  read-only, or every write is silently rejected.

## [1.2.3] — 2026-09-24

### Fixed

- Switching something on and quickly back off could drop the second
  command, leaving it on.

### Added

- Documented running the bridge as a standalone Docker container for HA
  Container installations.

### Internal

- Fixed two flaky tests and a login race in the e2e harness.

## [1.2.2] — 2026-09-17

### Fixed

- A disconnected `u8`/`s8` sensor shows unavailable instead of `255` or
  `-128`.

### Added

- A write is skipped when the controller already has that value (an
  automation re-asserting it, or a re-delivered MQTT command). Buttons
  always go through.

## [1.2.1] — 2026-09-17

### Fixed

- "Production (PV Power)" (29258) showed values 100 times too large; the
  firmware declares the wrong divisor
  ([#84](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/issues/84)).

---

## [1.2.0] — 2026-09-15

### Added

- 522 more settings and sensors placed in the Nibe Menus dashboard,
  cross-checked against the firmware, NIBE's Modbus register document and
  the firmware changelog.
- Large menus (1, 4, 7 and 3.1) split into a summary page with links to
  their sections (HA 2024.8+).

### Fixed

- A number showing an out-of-range value (such as an unconfigured zone's
  `0` °C) is unavailable instead, so HA no longer logs a warning every
  poll.
- Removed menu notes claiming a register doesn't exist "in this
  firmware", which was only known for one installation
  ([#82](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/issues/82)).

---

## [1.1.9] — 2026-09-13

### Changed

- The AppArmor profile is enforced instead of only logging; the gaps that
  surfaced (directory entries, `/translations`, the pytest cache) are
  fixed.

### Fixed

- Points 25165/25166 no longer log "unit overridden" on every start; the
  firmware reports their unit correctly now.

---

## [1.1.8] — 2026-09-11

### Fixed

- A point missing from a poll, as during a firmware update, deleted its
  entity. It now shows unavailable and is only removed after 5 minutes of
  absence.
- Points that return are re-enabled again after a restart; the list of
  wanted points used to be empty after every restart.

### Upgrading

- Entities already lost this way can't be restored by the update: restore
  a snapshot or re-enable them in the card. Search the log for
  `absent from bulk data` to find them.

---

## [1.1.7] — 2026-09-09

### Fixed

- Switching several things quickly no longer makes a toggle flip back on
  its own.
- The add-on stops cleanly instead of being killed during shutdown.
- An unexpected error can no longer leave the bridge deaf to Home
  Assistant while looking healthy.
- State files in `/data` can no longer be left empty by a crash mid-write.
- Ghost entities and missing attributes after a broker hiccup are gone.
- A snapshot name with a double quote no longer breaks the restore dialog.
- "Run Test Suite" no longer writes test data into the add-on's own
  state.
- A test run can always be stopped.

### Changed

- Clearer post-write scan logging, and log lines appear in real time.
- An unrecognised applied-mode record is ignored instead of treated as a
  mode change that disables hand-enabled entities.

### Upgrading

- If you ever pressed "Run Test Suite" on a live installation, nothing is
  needed: the stored mode repairs itself on the next restart.

---

## [1.1.6] — 2026-09-07

### Added

- "Req. op. mode (SG Ready)" (10614) is a dropdown: Cut off / Standard /
  Encouraged / Ordered
  ([#35](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/issues/35)).
  Its entity id changes from `number.` to `select.`.
- Built-in value labels (Off/On/Active/Passive, …) are translated for the
  12 supported languages
  ([#39](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/issues/39)).

## [1.1.5] — 2026-09-05

### Fixed

- 15 points that were shown as always-on binary sensors are sensors now:
  242–245, 632–638, 998, 2804, 3292 and 24961
  ([#35](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/issues/35)).
  Their entity ids change from `binary_sensor.` to `sensor.`.
- Removed a wrong value mapping on point 3292.

### Added

- A binary sensor that reports a value other than 0/1 is turned into a
  sensor automatically, with a warning in the log.

## [1.1.4] — 2026-09-04

### Fixed

- "Operating mode PV panels" (1021) was shown as an always-on binary
  sensor.
- Card search on an id or register matches exactly or by prefix, so
  `1021` no longer finds `11021`.

## [1.1.3] — 2026-09-02

### Fixed

- The card no longer crashes on a malformed snapshots or changelog
  message.
- "Run Test Suite" no longer fails four tests on real hardware.

### Changed

- Removed the card's controller model text, which never updated.

---

## [1.1.2] — 2026-09-02

### Fixed

- Wrong MQTT credentials stop the add-on with a clear error instead of
  retrying forever.
- The bridge's availability comes back after a reconnect with no entities
  enabled.

## [1.1.1] — 2026-08-26

### Fixed

- A point whose entity type changed no longer leaves a dead duplicate
  entity behind
  ([#23](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/issues/23)).

## [1.1.0] — 2026-08-24

### Added

- A point you enabled is re-enabled automatically when it returns after
  disappearing
  ([#21](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/issues/21)).

### Fixed

- A point that first appeared outside a post-write scan can still be
  linked to its controller later.
- The dynamic points notification points at the right dashboard in every
  mode.
- Option descriptions using `:` instead of `=` are parsed, so those points
  get their dropdown.
- Corrected the documented range of "Affect hot water" (3281).

### Changed

- Mutation-testing pass over all modules; dependency updates; documentation
  fixes.

---

## [1.0.7] — 2026-08-20

### Fixed

- Only one request at a time is sent to the controller, which fixes
  connection drops on some controllers.

---

## [1.0.6] — 2026-08-20

### Added

- `language` option: entity names in the controller's language (`auto`
  follows Home Assistant).
- `mode_switch_behavior` option: `replace` or `merge` the enabled entities
  when changing `mode`.
- Configuration UI translations for French, Spanish, Italian, Czech and
  Finnish.

### Fixed

- "Operating mode internal add. heat" (1760) was a binary sensor instead
  of a 4-state sensor.
- A select no longer drops a command after a language change.
- DOCS.md had the `secrets.yaml` priority backwards.
- "Test API Connection" uses the same TLS settings as the real connection,
  and reports combined failures correctly.
- Unchanged attributes are no longer republished every poll.
- A point with id 0 is no longer dropped from the dashboards.
- A failed check no longer stops the dashboard from ever being created.
- A killed test run no longer reports a misleading error.

---

## [1.0.5] — 2026-08-18

### Added

- "Test API Connection" button and result sensor, to tell network, TLS
  and credential problems apart.
- The "API Unreachable" notification shows the actual error.

### Fixed

- A controller unreachable at startup no longer creates a duplicate HA
  device.
- A select's state could disagree with its option list.
- Several thread-safety gaps.
- "Run Test Suite" could fail on permissions, run twice, or send real
  alarm notifications from test data.
- Debug entities follow `debug_mode` alone.
- The self-signed TLS fallback no longer weakens TLS beyond accepting the
  certificate.
- The test report link always shows the latest run.

### Changed

- Option values are validated in the bridge as well as by HA's schema.

---

## [1.0.4] — 2026-08-16

### Fixed

- Discovery configs are no longer all republished on every restart.
- `NIBE_LOG_LEVEL`/`NIBE_MODE` are honoured when running the container
  directly.
- "Mark all read" in the changelog can no longer be undone by a reconnect.

---

## [1.0.3] — 2026-08-15

### Fixed

- A configured `mqtt_host` is no longer replaced by `core-mosquitto`
  when the Mosquitto add-on is installed.
- Several thread-safety fixes, including "Flush Dynamic Map".
- "Run Test Suite" can be stopped cleanly, finds Python on Alpine, and
  runs on its own executor.

### Added

- Dependabot and CI.

[1.0.3]: https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/releases/tag/v1.0.3

---

## [1.0.2] — 2026-08-14

### Fixed

- Stopping the add-on during a test run no longer gets it killed, and an
  aborted run is no longer reported as failed.

[1.0.2]: https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/releases/tag/v1.0.2

---

## [1.0.1] — 2026-08-14

### Fixed

- The Entity Manager card file is copied to the right `www` folder.

### Changed

- Code split into smaller modules; relicensed under `LICENSE.md`.

[1.0.1]: https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/releases/tag/v1.0.1

---

## [1.0.0] — 2026-07-23

Initial public release.

[1.0.0]: https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/releases/tag/v1.0.0
