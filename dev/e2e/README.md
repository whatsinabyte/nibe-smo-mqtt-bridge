# Real-stack end-to-end harness (manual, on-demand — not CI)

This directory is a throwaway dev harness that proves the full loop actually
works: a real Home Assistant instance, a real Mosquitto broker, the actual
bridge built from this repo's own `Dockerfile`, and the actual Lovelace card
(`app/nibe-entity-manager-card.js`) loaded through HA's real frontend in a
real Chromium browser driven by Playwright.

It is **not** wired into CI and is never run automatically. Nothing here
touches `app/tests-js/` or `.github/workflows/tests.yml`.

## What this proves that the other two suites don't

- The pytest suite's MQTT-broker integration tests (`test_mqtt_broker_integration.py`)
  exercise the bridge against a real broker, but there is no real Home
  Assistant frontend involved — nothing verifies that a browser's real
  `hass.connection` / `hass.callService` actually behaves the way the card
  assumes.
- The card's own Vitest/Playwright suites drive the *real* card element
  against a *stubbed* `hass` object (`app/tests-js/support/fake-hass.js`) —
  thorough for the card's own logic, but the stub is still someone's
  approximation of what real HA does.
- **This harness is the only place both sides are real at once**: a real
  browser, logged into a real HA instance, clicking a real card, which calls
  the real `hass.callService('mqtt', 'publish', ...)`, which round-trips
  through a real Mosquitto broker to the real bridge process (built from
  this repo's actual `Dockerfile`), which really enables a point and
  publishes a real MQTT discovery config — and the test then confirms,
  via HA's own REST API, that a genuinely new HA entity exists and is not
  `unavailable`.

Scope is deliberately narrow: one happy path (enable one disabled entity,
confirm the corresponding HA entity appears). It is not meant to replace
either existing suite's coverage.

## Prerequisites

- Docker Desktop / a working `docker compose` (this was developed and
  verified against Docker Compose v2 via `docker compose`, using Colima on
  macOS — any Docker Engine with the compose plugin works).
  **Intel Mac / MacPorts Colima note:** MacPorts' `docker` port does not
  ship the v2 `compose` CLI plugin — only the standalone `docker-compose`
  v1.29.2 binary, which cannot parse this directory's `docker-compose.yml`
  (v1 rejects the top-level `name:` key, which is v2-only). Fix: download
  the v2 plugin binary and drop it where the Docker CLI looks for plugins:
  ```bash
  mkdir -p ~/.docker/cli-plugins
  curl -sL -o ~/.docker/cli-plugins/docker-compose \
    "https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-darwin-x86_64"
  chmod +x ~/.docker/cli-plugins/docker-compose
  docker compose version   # should now print v2.29.7
  ```
  After that, `docker compose` (space syntax, as used throughout `run.sh`
  and this README) works unmodified — no changes needed to `run.sh` itself.
  **Colima resource sizing:** the default Colima profile (2 CPU / 2GiB RAM)
  is not enough to run HA + the bridge + Mosquitto + the mock API + a
  headless Chromium at once — under that default, this harness produced
  flaky failures that looked exactly like application bugs (frontend/card
  not rendering in time, login/websocket glitches, even the Docker daemon
  itself becoming unresponsive under load) but were actually the VM
  starving. Give it more room before running this harness:
  ```bash
  colima stop
  colima start --cpu 4 --memory 4
  ```
  4 CPU / 4GiB is confirmed sufficient; this is a one-time host-level
  change (`colima list` shows the current allocation), not a repo change.
- Node.js (for Playwright). From the repo's `dev/e2e/` directory:
  ```bash
  npm install
  npx playwright install --with-deps chromium
  ```
- `reference-dumps/all_points_en.json` must exist at the repo root (it's
  gitignored, developer-local reference data — see `CONTRIBUTING.md`). If
  your worktree doesn't have it, copy it from your main checkout's own
  `reference-dumps/` directory.

## What's in here

| Path | Purpose |
|---|---|
| `run.sh` | One-command runner — wraps everything below into a single script. `./run.sh -h` for usage. |
| `docker-compose.yml` | Brings up mosquitto, the mock Nibe API, HA, the one-shot HA seeder, and the bridge itself (built from the repo's real `Dockerfile`). |
| `mock-api/` | Minimal stdlib-only HTTPS server replaying `reference-dumps/all_points_en.json` in the exact shapes `app/nibe_api.py` expects (self-signed TLS, any Basic auth accepted). Also exposes test-only control channels (host port `18443`), not part of the real Nibe API surface: `POST /mock-control/points/{id}` to change or inject a point's value across polls, `POST /mock-control/hidden/{id}` to withhold a point from the bulk response, and `POST /mock-control/alarms` to set the active alarm list `GET /notifications` serves (cleared again by `DELETE /notifications`, the bridge's Reset Alarms). |
| `mosquitto/mosquitto.conf` | Anonymous-auth Mosquitto config — dev only. |
| `bridge/options.json` | Mounted at `/data/options.json` — the standard HA add-on config path `load_config()` reads first; points the bridge at the mock API and the compose-network broker. `language` is `"nl"` (not `"en"`) specifically so `tests/translation.spec.ts` can prove the bridge's own hardcoded-label translation reaches a real entity — safe for the other specs too, since none of them assert on literal English label text, only entity existence/domain. |
| `fake-supervisor/` | A small aiohttp service standing in for the HA Supervisor under the compose hostname `supervisor`, so the bridge's hardcoded `ws://supervisor/core/websocket` and `http://supervisor/core/api/...` reach this harness's real Home Assistant. Opt-in per spec — see "Testing the Supervisor-only parts" below. |
| `ha-seed/` | `configuration.yaml` (+ YAML-mode Lovelace dashboards) and `seed_ha.py`, a one-shot container that headlessly onboards HA and configures the MQTT integration via HA's real REST APIs. |
| `tests/enable-entity.spec.ts` | Happy path: enable one disabled entity, confirm it appears in real HA. |
| `tests/binary-sensor-reclassification.spec.ts` | Enables the first working candidate from a list of genuinely auto-detected binary_sensor points, then uses the mock API's control channel to change its raw value to a non-boolean one, and confirms via HA's own REST API that the old `binary_sensor.nibe_*` entity disappears and a new, available `sensor.nibe_*` entity takes its place — proving `nibe_entity_manager.py`'s dynamic `_reclassify_binary_sensor` is visible correctly in a real HA entity registry, not just at the MQTT-message level. It then reloads the dashboard and requires the card to list the point as a sensor too. HA delivered the retained `all_metadata` to the reloaded card after the point's own `meta/{id}`, so the bridge has to keep `all_metadata` current as well (`_publish_point_catalog`), not just republish the per-point topic. |
| `tests/translation.spec.ts` | Enables point 3292, whose raw value in this dump maps to the English label `"On"` via `nibe_entity_detection.py`'s hardcoded `VALUE_MAPPINGS`, and confirms via HA's own REST API that the real published state is the Dutch translation `"Aan"` — proving the translation added for issue #39 reaches a real entity, not just the mocked pytest suite. |
| `tests/disable-entity.spec.ts` | Disables an enabled entity via the card and confirms HA's own REST API 404s it afterwards, and that the point leaves `/data/wanted_points.json` — an explicit disable is an intentional override, so the reactive safety net must not re-enable it behind the user's back. |
| `tests/switch-write.spec.ts` | Turns a real HA switch on and confirms the write reaches the mock controller and the new value comes back through the next poll — the full `optimistic: false` round trip, including the pending-write guard. |
| `tests/snapshot-restore.spec.ts` | Saves a snapshot from the card, changes the enabled set, then restores it and confirms the captured selection is what HA ends up with. |
| `tests/sg-ready-dynamic-discovery.spec.ts` | Writes to the SG Ready API-activation switch and confirms the points it unlocks (3260/10614 — absent from this dump entirely, injected via the mock's control channel) surface correctly classified and translated, with the changelog recording the appearance. |
| `tests/sg-ready-lifecycle.spec.ts` | Runs right after the discovery spec and takes the same three points through their whole lifecycle: disappearing and reappearing at runtime, then going away while the bridge is down (10613 switched off and the points withdrawn in the mock between a stop and a start). Requires the entities to be gone after the restart — the startup reconciliation used to leave their retained discovery configs behind as ghost entities — and checks after every step that the entity stats sensor's per-type counts still add up to its enabled count. |
| `tests/supervisor-menus-dashboard.spec.ts` | Brings the bridge up in `menus` mode through the fake Supervisor, then disables and re-enables one menu point and reads the saved dashboard from HA's own `.storage`: after the disable it must no longer reference the deleted entity, and after the re-enable it must reference the new one. Both were races between the dashboard regen and the registry watcher's debounced refresh. Puts the bridge back to the harness default afterwards. |
| `tests/menus-nested-dynamic.spec.ts` | Seeds a nested dynamic chain through the retained dynamic-map topic (switch 3846 shows select 3933, which shows sensor 248 — real points the mock serves) and requires 248's entity on the saved Nibe Menus dashboard. A dynamic point is only ever injected below its controller's row, and a dynamic controller has no row of its own, so 248 used to appear nowhere. Also pins the startup regression this spec first surfaced: the map's entries were judged against the static baseline, which leaves dynamic points out, so 3933's entry was marked firmware_removed although the controller served it and 248 was never even enabled. Also requires no injected row to show a "0 – 0" range — 248 declares `min == max` (no bounds), and used to read "Fan speed · 0 – 0 %". Then disables controller 3846 through the real disable command and requires 3933 and 248 to stay on the dashboard, under a row naming the controller's current value — they are live entities, and used to vanish with the controller's. Restores the original map afterwards. |
| `tests/changelog-seq-restart.spec.ts` | Marks the changelog read through the real management button until the retained history's `_seq` is at least 2, restarts the bridge, marks it read again and requires the new `_seq` to continue past the old one. The card discards any history whose `_seq` isn't above the last it applied, and the bridge used to restart the counter in every process, so a card left open across a restart ignored changelog updates until reloaded. Asserted on what the bridge publishes, since restarting a container reloads the page in this harness (see ARCHITECTURE.md §6a). |
| `tests/search-respects-filters.spec.ts` | Sets the card's type filter to Switch, searches an ID prefix (picked at runtime) that switches share with other types, and requires both the rendered rows and the "Select All" selection to hold only switches. A search match used to return before the dropdown filters were applied, so they were ignored while searching — and a bulk Disable after Select All deleted entities the filters had hidden. |
| `tests/dynamic-select-reappears.spec.ts` | Seeds the same chain as `menus-nested-dynamic.spec.ts`, withholds select 3933 from the mock for the bridge's start (so its dynamic-map entry is marked firmware_removed, which is correct at that point), then returns it and requires the retained map to show the entry restored within a few polls. Restoring used to happen only at startup, so a dynamic select hidden at the last restart stayed "removed" — learned outcomes ignored, never probed — until the next one. |
| `tests/debug-unplaced-view.spec.ts` | Runs the bridge in `menus` mode with `debug_mode` on (`bridge/options-menus-debug.json`) through the fake Supervisor and requires the saved dashboard's debug "Unplaced" tab to list writable point 6016 with "no declared range". Writable registers declaring `min == max` (no bounds) used to be skipped along with read-only ones. |
| `tests/card-reconfigure.spec.ts` | Sets a filter and opens the changelog modal in the real card, then calls `setConfig()` on the live element — the call Lovelace makes when it re-applies a card's config — and requires the table, the filter dropdown and the open modal to survive. The card used to rebuild its DOM and leave an empty table, blank controls and a vanished modal; the likeliest cause of the intermittent changelog-modal failure in `sg-ready-dynamic-discovery.spec.ts`. A second test does the same with an entity's details modal open, which must stay open on the same point, and a third with the mobile filter panel open, which must stay open and close on a single tap. |
| `tests/snapshots-mode-change.spec.ts` | Opens the snapshots modal in the real card, then delivers a mode change the way the card receives it — the retained `nibe/browser/applied_mode` message the bridge publishes after a mode switch, sent through HA's MQTT service — and requires the open modal to switch to its "restore is disabled" warning with Restore disabled, and back. A real mode switch means restarting the bridge, which reloads the page in this harness and would close the modal first. |
| `tests/menus-entity-rename.spec.ts` | In `menus` mode through the fake Supervisor, renames one of the menu dashboard's entities through HA's own registry API and requires the saved dashboard to follow to the new entity_id; renames it back afterwards. A rename carries no unique_id and doesn't change the enabled set, so the dashboard used to keep the old id until a restart. |
| `tests/absence-after-reenable.spec.ts` | Hides point 4 (the mock's control channel), disables its entity during the absence, returns and then hides the point again, and once the original first miss is past the 5-minute grace re-enables the entity while the point is absent — then requires it to survive the next polls as unavailable. The absence record used to survive the disable, so that absence was measured from the stale first miss and the entity was deleted on the next poll. Runs about seven minutes, past the real grace period. Tracks the entity by entity_id: HA drops its extra attributes (point_id included) while it is unavailable. |
| `tests/alarm-notifications.spec.ts` | Drives the controller's alarm list through the mock's control channel (`POST /mock-control/alarms`) with the bridge behind the fake Supervisor, and reads Home Assistant's own persistent notifications. A second alarm appearing while one is active must update the notification (it used to keep listing only the first), and a notification left from before a restart must be cleared once the alarms are gone (it used to stay until dismissed by hand). |
| `tests/stale-notifications-after-restart.spec.ts` | Stops the mock API, starts the bridge behind the fake Supervisor so it raises "Started Without Device" and "API Unreachable", then restarts the bridge with the mock back up and requires both notifications to be gone. Only the process that raised them used to know they were showing, so they stayed after a restart. |
| `tests/broker-state-loss.spec.ts` | Restarts the harness's non-persistent Mosquitto and requires point 4's discovery config, the card's point list and enabled set, and the applied mode to come back on their own; then stops HA, removes the config from the broker, starts HA and requires its MQTT birth message to bring the config back. Reconnecting used to restore only subscriptions, availability and states. |
| `tests/ha-disabled-while-stopped.spec.ts` | Stops the bridge, disables point 4's entity in HA's entity registry, and starts the bridge behind the fake Supervisor: the bridge must mirror the disable (point 4 leaves its enabled set). Without an event to see, it used to keep the entity enabled while HA had it disabled. |
| `tests/dynamic-disable-reversed.spec.ts` | Seeds a dynamic point (3933, shown by 3846) through the retained dynamic-map topic, disables its entity in HA's registry with the bridge behind the fake Supervisor, and requires the bridge to re-enable it (`disabled_by` back to null) — as DOCS.md promises. It used to only republish the discovery config, which leaves the registry's disabled_by untouched. Then waits for HA's echo of that re-enable and requires the notification to still explain the disable — the echo used to be read as the user re-enabling the entity. |
| `tests/snapshot-feedback.spec.ts` | Fills the snapshot list to its limit of 10 through the bridge's command topic, saves one more from the real card, and requires the card to show the bridge's refusal ("Maximum…") from `nibe/browser/snapshots/result`. Outcomes used to be only logged, so the card showed "Saving…" and then nothing. Deletes the snapshots it created. |
| `tests/select-unknown-value.spec.ts` | Enables point 3751 (Operating mode: Auto / Manual / Additional heat only), has the mock report 7, and requires HA to show the select unavailable — then `Manual` again once the mock reports 1. The raw value used to be published, which HA rejects while it keeps showing the last valid option. |
| `tests/snapshot-mode-recorded.spec.ts` | Recreates the bridge (a fresh container has no `/data/applied_mode`, only the retained `nibe/browser/applied_mode`), saves a snapshot through the command topic, and requires it to record that mode. Saving read the file alone and recorded `unknown`. |
| `tests/dynamic-disabled-while-stopped.spec.ts` | Seeds dynamic point 3933 (as `dynamic-disable-reversed.spec.ts`), stops the bridge, disables its entity in HA's registry, and starts the bridge behind the fake Supervisor: the startup registry check must re-enable it. It skipped dynamic entities, so this one stayed disabled. |
| `tests/ha-disabled-controller-down.spec.ts` | Seeds dynamic point 3933 (as `dynamic-disable-reversed.spec.ts`), stops the bridge, disables its entity in HA, stops the mock API and starts the bridge behind the fake Supervisor; once discovery is deferred ("Started Without Device") and the registry watcher has started, brings the mock back. The entity must be re-enabled in HA and kept in the bridge: the startup check ran before deferred discovery had reactivated it and was never repeated. Then requires the notification to still explain the disable 90s later (the re-enable runs off the watcher thread, so HA's echo can arrive before the bridge has noted it as its own). |
| `tests/card-sort-toggle.spec.ts` | Clicks a column header of the real card twice and requires ascending, then descending. Lovelace detaches and re-inserts the card while laying out the view, and every reconnect re-attached the click listeners — two handlers per control, so the second click never flipped the direction (and the mobile filter toggle opened and closed in one tap). |
| `tests/flush-restore-absent-point.spec.ts` | `@slow`. Withholds point 4 until the absence grace period disables it (still wanted, so its return would re-enable it), saves a snapshot without it and restores that replacing the selection, then brings the point back: it must stay disabled. The restore only un-wanted points it disabled itself, so this one came back. |
| `tests/restore-guard-known-mode.spec.ts` | Saves a snapshot, puts the bridge in menus mode, recreates it (a fresh container: no `/data/applied_mode`, the mode comes from the retained topic) and sends a restore: the bridge must refuse it. The guard read the missing file only, and restored. Back to the harness default afterwards. |
| `tests/flagged-holding-write.spec.ts` | Enables 3478 "Reset alarm" (a holding register the firmware flags `isWritable: false`) and presses its button through HA: the bridge must send the write to the controller (the mock answers "read only value") instead of refusing it itself ("Point 3478 is not writable"), which every press used to end in. Also requires the resulting "Write Failed" text (read from the non-retained bridge alert) to name the setting — it read the title from a key that never exists and always said "point 3478 (point 3478)". |
| `tests/absence-after-write.spec.ts` | Enables and writes 3751, then withholds point 4 inside the bridge's 90s post-write window: point 4's entity must go unavailable and stay, not be deleted. Any point missing in that window used to be taken for a dynamic point the write had hidden and disabled at once. |
| `tests/secrets-yaml-comment.spec.ts` | Writes a `secrets.yaml` with `nibe_basic_auth: <token>  # controller login` into HA's config (the bridge reads it as `/homeassistant/secrets.yaml`), starts the bridge without Nibe credentials in its options, and requires the mock to have received exactly `Basic <token>` (`GET /mock-control/last-request`). The comment used to be sent along as part of the credential. Removes the file afterwards. |
| `tests/language-auto-norwegian.spec.ts` | Sets Home Assistant's own language to `nb` (Norwegian Bokmål), starts the bridge with `language: auto` behind the fake Supervisor, and requires its requests to ask for `no` — the code the bridge's language list and translations use. HA's code used to be passed through unchanged. Restores HA's language afterwards. |
| `tests/ui-credentials-win.spec.ts` | Writes a different `nibe_basic_auth` token to `secrets.yaml` while the options keep their dev/dev credentials, and requires the mock to receive dev/dev — DOCS.md: secrets.yaml only fills in what the UI leaves blank. The secrets token used to win. Removes the file afterwards. |
| `tests/test-runner-dist.spec.ts` | Starts the bridge with `debug_mode`, presses the "Run Test Suite" button through its MQTT topic, and reads the running pytest command line inside the container: it must carry `--dist=loadscope`. Without it, test classes sharing one `/tmp` path race across xdist workers and the button reports failures for a healthy bridge. Recreating the bridge afterwards aborts the run. |
| `tests/block-freq-switch.spec.ts` | Enables point 4970, "Block freq 1 active (EB101)", and requires a `switch.` entity. It was overridden to number on a "frequency value" claim its 0–1 range rules out, while the identical EB102–EB108 flags were switches. |
| `tests/defrost-requested-switch.spec.ts` | Enables point 8060, "Defrost requested (EB101)", a 0–1 holding register that is s8 rather than u8, and requires a `switch.` entity. The switch auto-detection only accepts u8, so it was a 0–1 number. |
| `tests/legionella-min-temperature.spec.ts` | Enables point 3702, the anti-legionella stop temperature, and requires HA's number entity to start at 55 °C. The firmware declares a raw minimum of 55 with divisor 10 (5.5 °C), a factor of ten too low; corrected by `RANGE_OVERRIDES`. |
| `tests/unit-override-families.spec.ts` | Enables 821 (blank time left, charge pump 5) and 5298 (DM start, priority 1 smart energy source) and requires units `s` and `DM`. The firmware reports none; `UNIT_OVERRIDES` covered only pumps 1–4 and the read-only DM values. |
| `tests/menu-documented-units.spec.ts` | Enables 995, 3282, 4030 and 14314, which the firmware reports without a unit, and reads back from Home Assistant's own state that it accepted the unit the installer menus give (bar, °C, min, W) and, for the sensors, the matching device class (pressure, temperature). |
| `tests/verified-option-lists.spec.ts` | Enables 7022 (Blocking actions, ERS 3) and 4692 (Charging method) and requires selects with 3 and 2 options. The firmware gives them no description, so they were a bare 0–2 number and a switch; their option lists, verified on a live controller, are now hard-coded. |
| `tests/version-sensors.spec.ts` | Serves the reference installation's raw version values and requires Home Assistant to show what myUplink shows: 2509 → 4.13, 2453 → 3.3.1, and the inverter version 14987 as plain 61 (it used to be decoded like 2453, showing 0.0.61). |
| `tests/acs-status-sensor.spec.ts` | Enables 2701 "Status (ACS)", boolean-shaped in the firmware but mapped 3 = Passive / 7 = Active, and requires a `sensor.` entity. It used to be discovered as a binary_sensor and only reclassified once a real ACS reported its first value. |
| `tests/operating-prio-labels.spec.ts` | Steps 56150 "All sub units operating prio" through 0, 1 and 2 and requires Home Assistant to show idle, heating and cooling (in the harness's Dutch: In rust, Verwarmen, Koelen). It used to map 10/20/30/40/60, values a live controller doesn't report, so it showed a bare number. |
| `tests/safety-read-only.spec.ts` | Enables 55749 (Block new compressor) and 55884 (Set point value power) and requires plain `sensor.` entities. DOCS.md lists both as intentionally unexposed; the first rested on the firmware's `isWritable` flag (no longer relied on), the second was a writable number all along. |
| `tests/bridge-dashboard-recovery.spec.ts` | Recreates Home Assistant with `ha-seed/configuration-storage-bridge-dashboard.yaml` (via `HA_CONFIGURATION`; identical except that `nibe-bridge` isn't a YAML dashboard), creates `/nibe-bridge` as an empty storage dashboard — the state a create whose config save failed leaves behind — and requires one bridge start through the fake Supervisor to write the Entity Manager card into it. Puts both containers back afterwards. |
| `tests/remove-frontend-menus.spec.ts` | Runs the bridge in `menus` mode with `remove_frontend: true` (`bridge/options-menus-remove-frontend.json`) through the fake Supervisor, stops it the way the Supervisor does, and requires the Nibe Menus dashboard to be gone from HA's dashboard list. Also requires the debug test report it placed in `/config/www` beforehand to be gone (it used to be left behind). The teardown clears every retained topic the bridge owns, so the bridge is put back in essential mode afterwards, which rebuilds its entities. |
| `tests/transient-absence.spec.ts` | Withholds an enabled point from the mock's bulk response for four consecutive polls (`POST /mock-control/hidden/{id}`) and confirms the entity goes `unavailable` but is **not** deleted, stays enabled in the card, stays in `/data/wanted_points.json`, and recovers by itself once the point returns — the regression behind the 4.13.12 incident, where an incomplete point list served during a controller restart destroyed thirteen entities. The complementary "still absent after five minutes, so really disable it" branch is covered by pytest with a patched clock rather than by a five-minute e2e run. |
| `tests/restart-survival.spec.ts` | Enables a point the configured mode would not have, restarts the bridge, and confirms every entity comes back — same object_ids, the manual addition live again, and still recorded as wanted. Covers ARCHITECTURE.md §4.4's central claim ("the bridge survives restarts without losing user customisations"), whose restore path no other spec enters. Compares object_id rather than entity_id, since a reclassified point legitimately changes domain. |
| `tests/controller-outage.spec.ts` | Stops the mock API entirely and confirms the "API Reachable" management binary_sensor goes off after `api_failure_threshold` failed polls, that **no entity is deleted** while the controller is merely unreachable, and that recovery is automatic. Pins the otherwise-unwritten requirement that a failed bulk fetch leaves `bulk_data` intact — if it ever cleared it, every point would look absent and `_ABSENT_GRACE_S` would delete the user's whole entity set. Deliberately does *not* assert entities go `unavailable`: by design they hold their last value. |
| `tests/device-identity.spec.ts` | Restarts the bridge while the controller is unreachable and confirms the serial-derived `device_id` persisted to `/data/device_id` is reused, so the management device keeps its identity instead of falling back to the generic default and orphaning a ghost device in HA (ARCHITECTURE.md §4.1). Asserts on the retained discovery payload's `device.identifiers` — with HA 2024.10 an already-registered entity is updated in place and a discovery config naming a different device is ignored, so entity ids, device association and the device registry all look identical either way; the wrong identity is only visible in what the bridge publishes, which is also what any fresh consumer would act on. |
| `tests/mode-switch-behavior.spec.ts` | Covers three promises at once: an ordinary same-mode restart must not run `apply_mode` (so manual Entity Manager additions survive), `merge` only ever adds, and `replace` disables what falls outside the new mode. Reads the exact enabled set from the retained `enabled_state` topic, and switches mode by mounting a different options file via `BRIDGE_OPTIONS` — not `NIBE_MODE`, which `run.sh`'s own `--mode` CLI flag outranks. |
| `tests/mqtt-callback-robustness.spec.ts` | Fires malformed payloads (bad numbers, wrong JSON shapes, invalid UTF-8) at every subscribed management topic, then proves the bridge is **still processing MQTT** by enabling a real entity over the same connection. Covers ARCHITECTURE.md §3's no-exception-escapes-a-callback invariant, whose failure mode leaves the container up and the poll loop logging normally while every command is silently ignored forever — verified by removing `_guard_callback` and watching exactly that happen. |
| `tests/entity-attributes.spec.ts` | Confirms an enabled entity carries the attributes DOCS.md promises (`point_id`, `modbus_register`, `writable`, `default_value`) with values checked against the firmware dump, including that `default_value` is divisor-applied "display units" rather than the raw register integer. The attributes are a separate retained payload from the discovery config and the state, so an entity can be entirely correct and still reach HA with none — and a template reading a missing attribute renders `None` rather than failing. |

## How the bridge runs without a Supervisor

The bridge is normally a Home Assistant Supervisor add-on. `run.sh` and
`generate_nibe_mqtt.py`'s `load_config()` already have documented dev/Docker
escape hatches for everything except the parts that are genuinely
Supervisor-only (the registry watcher, Lovelace provisioning, HA
notifications, base-URL/language auto-detection). Those degrade gracefully
without a Supervisor, and specs that need them run them through the fake
Supervisor — see "Testing the Supervisor-only parts" below.

This harness runs `run.sh` completely unmodified (`CMD ["/run.sh"]` in the
Dockerfile, untouched):

- `SUPERVISOR_TOKEN` is empty unless a spec opts in. `run.sh`'s MQTT
  auto-discovery-via-Supervisor-Services-API block only runs for
  `mqtt_host: core-mosquitto`, which this harness doesn't use, so it no-ops
  either way.
- Credentials (`nibe_username`/`nibe_password`) and every other option come
  from `/data/options.json` (mounted read-only from `bridge/options.json`),
  exactly like a real add-on install — `load_config()`'s documented
  priority order (`secrets.yaml` < `options.json` < env vars < CLI args)
  is honoured unmodified.
- `apk`/Alpine base image, `jq`, `curl` — all already in the Dockerfile;
  nothing extra was needed.

**No production code was changed.** `app/generate_nibe_mqtt.py`,
`app/nibe_api.py`, `app/nibe-entity-manager-card.js`, `run.sh`, and
`Dockerfile` are all byte-identical to what ships in the add-on.

## Testing the Supervisor-only parts

Three parts of the bridge only run inside a real Supervisor: the HA entity
registry watcher, Lovelace resource/dashboard provisioning (`nibe_lovelace.py`),
and HA persistent notifications. All three talk to Core through fixed
Supervisor addresses (`ws://supervisor/core/websocket`,
`http://supervisor/core/api/...`) using the `SUPERVISOR_TOKEN` the Supervisor
injects, and all three no-op cleanly without that token.

By default the bridge here gets no token, so they stay off and every spec
runs exactly as it always has: the card resource and the Nibe Bridge
dashboard come from YAML-mode Lovelace in `ha-seed/configuration.yaml`
instead.

A spec that needs them opts in by recreating the bridge with
`BRIDGE_SUPERVISOR_TOKEN=e2e-fake-supervisor-token` (see
`tests/supervisor-menus-dashboard.spec.ts`). That routes them through
`fake-supervisor/`, which does what the real Supervisor does: it checks the
bridge's token, talks to Core with its own long-lived token (created on
startup from the token the seeder writes), and relays — WebSocket messages
unmodified in both directions, so registry events reach the bridge exactly
as Core emits them. Anything it doesn't provide (e.g. `/services/mqtt`)
gets a 404, so `run.sh` keeps using the configured broker.

No production code is involved. One caveat: Lovelace resource registration
fails under YAML-mode Lovelace (resources are managed in YAML there); the
bridge logs that as a warning and carries on, and the dashboards themselves
are storage-mode and work normally.

The exception is the Nibe Bridge dashboard: here it is a YAML dashboard, so
the bridge always finds it "already existing" and never provisions it. A
spec that needs the bridge's own provisioning of it recreates Home Assistant
(same volume) with `HA_CONFIGURATION=./ha-seed/configuration-storage-bridge-dashboard.yaml`,
which differs only in leaving that dashboard out — see
`tests/bridge-dashboard-recovery.spec.ts`, which also deletes the storage
dashboard it ends up with before putting the default configuration back,
since the two would collide on `/nibe-bridge`.

## How HA is brought to a usable, unattended state

Home Assistant requires interactive onboarding (create the admin user) and an
interactive config-flow walk for the MQTT integration (YAML-configured MQTT
brokers were removed from HA years ago). `ha-seed/seed_ha.py` drives both
headlessly using HA's own public REST APIs — no `.storage` file hand-editing:

**Keep the pinned version current.** This harness pins
`homeassistant/home-assistant:2026.9.1`. The pin exists for reproducibility,
not to freeze a version: it sat on 2024.10.1 for two years, which meant the
suite was validating against something no user runs. Bumping it cost six
harness fixes (the seeder's MQTT config flow and auth, the login selectors,
HA's readiness signal, and stale-image builds — see ARCHITECTURE.md §6a) and
zero changes to the bridge. Expect a bump to break the seeder and the login
helper first; `seed_ha.py` now prints the config-flow schema HA asks for, and
the login sequence lives in one file (`tests/support/ha-login.ts`) rather than
in all thirteen specs.

1. Polls `/api/onboarding` until HA responds.
2. `POST /api/onboarding/users` to create the admin user, exchanges the
   returned `auth_code` for a bearer token via `/auth/token`.
3. Completes the `core_config`, `analytics`, and `integration` onboarding
   steps (all four are required before the frontend stops redirecting to
   `/onboarding.html`).
4. Drives the **mqtt** integration's real config-flow API
   (`POST /api/config/config_entries/flow` → `POST .../flow/{flow_id}`) to
   add an MQTT config entry pointing at the `mosquitto` service — this is
   the current, supported way to configure MQTT headlessly; hand-writing a
   `core.config_entries` entry into `.storage` was considered and rejected
   as version-fragile and undocumented, per the task's own guidance to
   prefer whichever technique is actually reliable.
5. Writes `seed-out/credentials.json` (username/password) and
   `seed-out/token.txt` (bearer token) to a **bind-mounted** host directory
   so the Playwright test (running on the host, not in a container) can
   read them.

**Re-running the seeder**: steps already done are skipped, and an already
onboarded instance is detected (HA stops serving `/api/onboarding` once
onboarding is complete) and simply logged into through HA's login flow — so
`docker compose run --build --rm ha-seed` against a running stack is safe,
e.g. to mint a fresh token. `run.sh` still starts from a clean `seed-out/`
every run.

**The token is long-lived** (created over HA's WebSocket API, which is why
the seeder image installs `websocket-client`). It used to be the 30-minute
access token from onboarding, so iterating on specs against a stack that
stayed up longer than that failed every spec at once with authentication
errors.

## How to run it

**One command**, from `dev/e2e/`:

```bash
./run.sh
```

This wraps every step below into one script: installs JS deps + the
Chromium browser on first run, always tears down and recreates the stack
from a clean slate first (see the idempotency note above — reusing a
stopped/restarted `ha-config` volume isn't supported, so every `./run.sh`
run is a genuinely clean one), brings up mosquitto/mock-api/HA, polls until
HA answers, runs the headless seeder, starts the bridge and waits for
"Bridge ready" in its logs, restarts HA once so it picks up the card JS,
runs the Playwright test, then tears the stack down again. Exits with the
test's own exit code.

```bash
./run.sh --keep-open   # skip teardown at the end, to poke around the running stack
./run.sh --down        # teardown only (drops the ha-config volume, clears seed-out/)
./run.sh -h             # usage
```

After a `--keep-open` run, HA is reachable at http://localhost:18123 with
the seeded admin credentials in `ha-seed/seed_ha.py`
(`HA_SEED_USERNAME`/`HA_SEED_PASSWORD` in `docker-compose.yml`).

### Running in parallel

The specs share one stack's state, so a single stack runs them one at a
time. To run them several at once, bring up extra identical stacks next to
run.sh's stack A and give Playwright one worker per stack:

```bash
./run.sh --keep-open        # stack A, if it isn't up already
./stacks.sh up 3            # stacks B and C (~2 min, in parallel)
E2E_STACKS=3 npx playwright test
./stacks.sh down            # B and C only
```

Each stack is its own compose project (`nibe-e2e-b`, …) with host ports
10000 apart (HA 28123 / 38123, …) and its own `seed-out-<letter>/`; every
worker is pinned to one stack by its `TEST_PARALLEL_INDEX`
(`tests/support/stacks.ts`, which sets the `HA_URL`, container-name and
compose variables the specs and helpers read). `E2E_STACK=<n>` runs on one
particular stack. Each stack needs about 0.5 GB of Docker memory.

**Order.** Specs that git reports as modified or untracked run first, most
recently edited first, so a broken new spec fails in the first minutes;
the rest follow longest first, from `test-durations.json` (written after
every run by `tests/support/duration-reporter.ts`), so the stacks finish
together. `E2E_ORDER=alpha` restores alphabetical order. Specs tagged
`@slow` (waiting out the real 5-minute absence grace period) can be left
out with `--grep-invert @slow`.

**Code without rebuilding.** The bridge container mounts `app/` over the
image's copy (`BRIDGE_APP_DIR`, default the repo's `app/`), so a code change
needs only a bridge recreate. Point `BRIDGE_APP_DIR` at another tree — a
snapshot, or a copy with a fix reverted for a negative control — to run
that code instead. With Colima the tree must be under your home directory:
other host paths (`/tmp`, for one) show up empty inside the VM. The image
needs rebuilding only for Dockerfile, requirements or `run.sh` changes.

### What each step actually does (what `run.sh` automates)

If you want to run a step manually — e.g. to poke at one stage without
rerunning the whole thing — this is the sequence `run.sh` performs,
unrolled:

```bash
# One-time: JS deps + browser
npm install
npx playwright install --with-deps chromium

# Bring up mosquitto, the mock API, and HA; wait for HA (~15-30s)
docker compose up -d mosquitto mock-nibe-api homeassistant
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:18123/   # poll until non-000

# Headlessly onboard HA + configure the MQTT integration
docker compose run --rm ha-seed

# Start the bridge (builds from the repo's real Dockerfile the first time)
docker compose up -d bridge
docker logs -f nibe-e2e-bridge   # wait for "Bridge ready — ..."

# HA only picks up new files under /config/www (the card JS the bridge just
# copied there) on (re)start — restart it once after the bridge is up:
docker restart nibe-e2e-homeassistant
sleep 15

# Run the test (HA is published on host port 18123, not 8123, to avoid
# colliding with a real HA instance you may already have running locally)
HA_URL=http://localhost:18123 npx playwright test
```

### Tear down

```bash
./run.sh --down
# ...or equivalently:
docker compose down -v   # -v also drops the ha-config volume — required for a clean re-seed
rm -rf seed-out/*.json seed-out/*.txt
```

### Expected runtime

- Image builds (first run only): ~2-3 minutes (bridge image reuses the repo's
  own Dockerfile and pulls the full dev/test Python dependency set).
- Stack startup + HA onboarding + bridge ready: ~30-45 seconds.
- The Playwright test itself: ~5-10 seconds.

## Verification performed

This harness was actually run, not just written. Confirmed for real, in
order, against a freshly-created `ha-config` volume:

- The mock API serves real firmware data over HTTPS with the exact
  `/api/v1/devices/0{,/points,/points/{id},/notifications}` shapes
  `app/nibe_api.py` expects (cross-checked against Nibe's own
  "Local REST API" spec PDF, not just the Python client code).
- `ha-seed` completed onboarding (`user`, `core_config`, `analytics`,
  `integration`) and the MQTT config-flow (`create_entry`) against a real,
  freshly-started HA container, entirely via REST calls.
- The bridge (built from this repo's actual `Dockerfile`, `run.sh`
  unmodified) started, fetched 1158 real points from the mock API, applied
  `mode: essential` (29 enabled / 1158 total — plenty left disabled for the
  test), and logged `Bridge ready`.
- `_copy_card_file()` copied the real card JS to the shared `/homeassistant/www`
  volume with no Supervisor token, confirmed served at `/local/nibe-entity-manager-card.js`
  after an HA restart.
- Lovelace auto-provisioning cleanly no-op'd exactly as documented above;
  the YAML-seeded "Nibe Bridge" dashboard rendered the real
  `nibe-entity-manager-card` custom element instead.
- **The Playwright test passed**: logged into the real HA frontend with the
  seeded admin credentials, opened `/nibe-bridge/entity-manager`, clicked
  `Enable` on real disabled points through the real card, and confirmed via
  `GET /api/states` that new HA entities appeared and at least one left
  `unavailable` (a few of these specific registers legitimately carry a
  firmware "sensor not connected" sentinel value in the real dump this mock
  replays — the bridge correctly reports those as unavailable, which is why
  the test tries a handful of candidates rather than asserting on exactly
  one specific point ID).

## Note on `app/tests-js/` (resolved)

This harness was built by an agent working in an isolated git worktree
checked out from the last commit — at that point `app/tests-js/` (the
Vitest+jsdom suite, 307 tests, plus its own Playwright smoke suite) existed
only as *uncommitted* changes in the main working tree, so the worktree
genuinely couldn't see it and reported it as absent. It exists and is
unrelated to this harness: `app/tests-js/` drives the real card element
against a *stubbed* `hass` object (see `app/tests-js/support/fake-hass.js`
and its own README section in `CONTRIBUTING.md`); this directory
(`dev/e2e/`) is the separate, heavier "everything is real" tier described
above, kept as its own standalone Node project (own `package.json`,
`playwright.config.ts`) rather than folded into `app/`'s tooling, since
it has a fundamentally different runtime shape (Docker Compose, a real HA
instance) and is not meant to run in CI.
