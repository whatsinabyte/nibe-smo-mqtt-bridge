# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Changed

- **Translations checked against NIBE's installer manuals** (SMO S40 or
  VVM S320) in Swedish, Norwegian, Danish, Finnish, German, Dutch,
  Italian, Polish and Czech:
  - SG Ready operating mode (3260) is now translated in those languages,
    in the manuals' names for the four states (Dutch Normaal / Blokkering
    / Lage prijs / Overcapaciteit, German Normalbetrieb / Blockierung /
    Niedriger Preis / Überkapazität, …). The mapping is now also confirmed on
    a VVM S320 (issue #40).
  - "Additional heat only" follows the manuals' operating mode menu
    (e.g. Swedish "Endast tillsats", Italian "Solo riscaldamento
    supplementare"), "Intermittent" the pump menus in Dutch, Norwegian
    and Polish, and "Price per kWh" menu 4.6 in Norwegian and Finnish.
  - Newly translated from the same manuals: hot water demand (Small /
    Medium / Large) and the hot water charging method (Target temp /
    Delta temp, where the manual has that menu).
  - Spanish and French had no manual to check against and are unchanged.
- **Romanian translation** (`language: ro`): the add-on settings screen,
  and the value labels NIBE's Romanian SMO S40 installer manual names —
  operating mode, SG Ready, hot water demand, flow setting and the like.
  Other labels stay English.

---

## [1.4.2] — 2026-10-24

AUX functions by name and SG Ready under NIBE's own names, both from an
S1155 owner's tests on his controller (issue #82), and the Language
setting for the six languages NIBE firmware 4.14.6 adds.

### Changed

- **AUX inputs and Output AUX1 become drop-downs naming their function**
  (menu 7.4): Input AUX1–9 (3958, 3957, 3956, 3955, 3954, 4713,
  55035–55037), "AUX from Modbus" (22070) and Output AUX1 (3959). As a new
  entity type they get **new entity ids**: repoint automations that use
  the old `number.` ids. A function whose name isn't known yet shows as its
  number, so every value stays selectable. AUX11 (12463) stays a number for
  now.
- **SG Ready operating mode (3260) uses NIBE's names** — Normal, Blocking,
  Low price, Overcapacity — instead of "Normal Price", "High Price", "Low
  Price" and "Free Electricity". All four states are now confirmed on a
  controller (issue #40).
- **Language (3745) covers the six languages of firmware 4.14.6**: Srpski,
  Ελληνικά, Português, Føroyskt, Shqip and Bosanski, values 25–30, while
  the metadata still declares 0–25. Before, selecting one made the select
  unavailable, so it could no longer be set back from Home Assistant. Their
  codes (`sr`, `el`, `pt`, `fo`, `sq`, `bs`) added to the `language` option.

### Fixed

- AUX notes in the menus no longer claim 0 = no function and 5 = alarm
  output for the AUX outputs; on an S1155, 0 is alarm output and 5 "not
  used". Menu 3.1.14 no longer claims the AUX assignments are unavailable
  through the local API.

---

## [1.4.1] — 2026-10-18

Ground-source and hot water follow-up, from an S1155 owner checking the
1.4.0 mappings on his own controller (issue #82).

### Fixed

- **Brine pump operating mode (5482) had the wrong options.** It is menu
  7.1.2.7 "Pump speed brine" — Manual / Auto / Fixed delta — not 7.1.2.6,
  whose Intermittent / Continuous / 10 days labels it carried: choosing
  "Continuous" in Home Assistant set the pump to Auto. Menu 7.1.2.6 now
  uses its own register (3753).

### Added

- **Hot water demand** (menu 2.2): Small / Medium / Large / Smart control,
  read and written from Home Assistant.
- **Start and stop temperatures per demand mode** (menu 7.1.1.1).
- **Maximum permitted speed** of the GP1 pump (menu 7.1.2.2).

In `menus` mode, newly mapped settings are not switched on by an update:
enable them in the Entity Manager card.

---

## [1.4.0] — 2026-10-17

A release about using the controller from Home Assistant: the Nibe Menus
dashboard gets real controls, settings say whether they still have their
factory value, and the first ground-source mappings arrive. Read
**Changed** before upgrading.

### Added

- **Nibe Menus dashboard: settings are tiles with their control** — −/+
  buttons for a number, in the setting's own step (0.5 °C for a room
  setpoint), a drop-down for a select, a toggle for a switch, a press
  button for a button. Above each, an info box gives its range, step and
  explanation; tips and warnings are shown too, and an empty line
  separates the settings. Time settings and read-only values stay rows.
- **Live "changed from default" status**: a number, select or switch with
  a factory default gets a `default_status` attribute — `default`, or
  `✏️ default: 20 °C` once it differs — shown on its tile. It follows the
  value, unlike the old ✏️ badge, which only updated with the dashboard.
- **A setting's step comes from the firmware** (`change` metadata) and is
  shown in the Entity Manager card's details panel.
- **Graphs and statistics for unitless counts**: compressor starts and
  sizes, compressor counts, fan speeds and similar get a state class, and
  running totals (compressor starts, operating hours) count up, so a
  statistics graph shows them per day, week or month. Codes keep their
  state bars.
- **A firmware update gets a changelog entry**, naming the version change
  and the registers it added, removed or redescribed.
- **A notification when entity ids change** after an update changed an
  entity's type, listing old id → new id.
- **Value label status**: a `mapping` attribute (`confirmed`, `partial`,
  `inferred`) on labels the bridge supplies itself, with a report link in
  the card for the unconfirmed ones.
- **Labels**: Priority (55000), charge pump (10897) and operating priority
  (14952) from NIBE's own option lists; SG Ready status (3260), PV panels
  operating mode (1021), frost protection (3097) and all sub units
  operating prio (56150); heat pump type 55 reads "S1155".
- **Ground-source (water/water) mappings** from an S1155's own dump (issue
  #82): GP1 pump speeds (menu 7.1.2.2), the brine pump's fixed delta
  (7.1.2.7) and the brine alarm settings (7.1.2.8).
- **Log lines carry the date.**
- DOCS.md: a section on getting more from the data (COP, energy log).

### Changed

- **Translations follow NIBE's own wording** where NIBE has one (e.g.
  Dutch "Verwarming", "Koeling", "Warmtapwater").
- **Menu explanations are split by purpose** — what a setting is, what to
  do with it, and the risk — for 65 settings, and menu 1.1 now starts with
  the zone 1 room setpoint (the older room sensor setpoints have no
  effect, as noted there).
- **Statistics**: sensors that gain a state class start long-term
  statistics, and the operating-hour counters change from `measurement`
  to `total_increasing`; Home Assistant may show a repair notice for the
  latter's existing statistics.
- On ground-source models, 4798–4800 (automatic GP1 speed) become
  switches and 671/2784 (compressor counts) sensors: new entity ids there.

### Fixed

- **CPU spikes every poll**: availability and unchanged states were sent
  again each poll — about 1,150 messages a poll in menus mode. Now only
  changes are sent (176 messages in three minutes against 13,859).
- The built-in test suite no longer sends notifications to Home Assistant
  or writes the installation's own `/data` files.
- Max brine in (menu 7.1.2.8) has the documented 10–30 °C range.
- Dashboard tiles no longer reference a `default_status` attribute their
  entity doesn't have (reported by Spook).

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
