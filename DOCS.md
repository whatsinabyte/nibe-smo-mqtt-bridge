# Nibe S-Series MQTT Bridge

Connects your Nibe S-series heat pump controller to Home Assistant via MQTT. Temperatures, pressures, operating states, energy data, and configurable setpoints appear as native HA entities — ready for dashboards, automations, and energy monitoring. No cloud account required.

---

## Quick Start

1. **Enable the local REST API** on your controller — go to **Menu 7.5.15** on the controller itself. Set a username and password. If this menu is not visible, contact your installer.
2. **Note the controller's IP address** — assign a static IP via your router to prevent it changing after a reboot.
3. **Install the Mosquitto broker** add-on from the HA Add-on Store and configure the **MQTT integration** under Settings → Devices & Services → Add Integration → MQTT.
4. **Install this add-on** from the Add-on Store.
5. **Configure the required fields** under the add-on Configuration tab:
   - `nibe_host` — IP address of your controller
   - `nibe_username` and `nibe_password` — the credentials you set in step 1
   - If you are using the official **Mosquitto broker** add-on, the bridge auto-discovers its hostname, port, and credentials — you do not need to fill in the MQTT fields. If you use a different MQTT broker, enter `mqtt_host`, `mqtt_port`, `mqtt_username`, and `mqtt_password` manually.
   - Leave everything else at the default to start
6. **Start the add-on.** The bridge fetches all available data points, creates two devices in HA (**your controller** and **Management**), and provisions the **Nibe Bridge** dashboard automatically.
7. **Open the Nibe Bridge dashboard** in the HA sidebar. Use the Entity Manager card to browse all available data points and enable the ones you want.

> 💡 Start with the default `essential` mode — it enables the most useful sensors immediately. Use the Entity Manager card to add more as you explore.

---

## Compatible Hardware

The bridge works with any Nibe S-series controller that exposes the local REST API — available on all S-series products from approximately 2019 onwards. Minimum supported firmware: **4.5.7**.

| Product group | Models | Connection | Status |
|---|---|---|---|
| Ground / water heat pumps | S1155, S1156, S1255, S1256 | Built-in (direct) | ✅ Direct |
| Ventilation heat pumps | S735 | Built-in (direct) | ✅ Direct |
| Indoor units / controllers | VVM S310, S320, S325, S500, SMO S40 | Built-in (direct) | ✅ Direct |
| Air / water outdoor units | S2125, F2120, F2040, F2006 | Via SMO S40 or VVM S-series | ✅ Via hub |
| Legacy ground/water (F-series) | F1145, F1155, F1245, F1255 | None — ebus only | ❌ Not supported |
| Legacy ventilation (F-series) | F370, F470, F730, F750 | None — ebus only | ❌ Not supported |
| Legacy indoor units | VVM 225, VVM 310, VVM 320, VVM 325, VVM 500 | None — ebus only | ❌ Not supported |
| Legacy controllers | SMO 20, SMO 40 | None — ebus only | ❌ Not supported |

> ℹ️ **Ground/water and ventilation heat pumps** (S1155, S1255, S1156, S1256, S735) have the controller built directly into the unit — no separate SMO S40 is needed. The SMO S40 is only required for air/water outdoor units (S2125, F2120, F2040) that have no integrated controller.

> ℹ️ **Air/water outdoor units** connect via the internal bus to an S-series controller. The bridge talks to the controller, which exposes the outdoor unit's data transparently — compressor frequency, outdoor temperature, and operating state all appear as if the unit were directly connected.

> ℹ️ **Legacy F-series** indoor units and older VVM controllers use an ebus architecture without a REST API and are not compatible with this bridge.

---

## Supported Accessories

When an accessory is physically connected to the controller's communication bus, its registers appear automatically in the REST API — and therefore automatically in HA. No configuration needed.

| Category | Accessory | What appears in HA |
|---|---|---|
| Climate / extra zones | AXC 30/40 zone modules | Up to 8 independent climate systems — supply temperature, room temperature, pump status |
| Domestic hot water | VST 11/20, extra boiler sensors | Hot water temperature (top/bottom), three-way valve status, legionella prevention setpoints |
| Ventilation | ERS 10/20/30, ALT 30 | Air flow rates, filter status, CO₂ levels, humidity, heat recovery efficiency |
| PV solar | EME 20 | Real-time PV output (W), daily yield (kWh), total production |
| Solar thermal | Solar 40/42 | Collector pump control, collector and tank temperatures |
| Energy metering | EMK 500 / EMK S | Thermal energy and flow measurement |
| Wireless room sensors | THS 10 | Temperature and humidity per room |
| Wireless CO₂ sensors | CDS 10 | CO₂, temperature, and humidity per room |
| Swimming pool | POOL 40 | Pool water temperature, heating pump status, pool valve position |
| Active cooling | ACS 45 | Dew point calculation, cooling capacity, passive vs active cooling status |
| Buffer tanks | VPA / VPB / VPAS | Tank temperature sensors and connection status |
| External heat source | AXC 30 (boiler) | Bivalent operation — boiler enable/disable, degree-minute threshold registers |

> ℹ️ **S-series air/water heat pumps** (S2125, F2120, F2040) have reversible cooling built directly into the unit — no ACS 45 is needed. The ACS 45 is only relevant for ground/water heat pumps (S1155, S1255, S1156, S1256).

> ℹ️ **Wireless sensors** (THS 10, CDS 10) connect via the controller's internal RF. They appear in the API as soon as the controller pairs with them.

> 💡 **Dynamic accessories** — registers that only appear while a feature is active are handled automatically. When you switch to manual mode, the manual setpoint registers appear in HA within ~60 seconds. When you switch back, they disappear. The Changelog in the Entity Manager card records every such change.

---

## Configuration

### Essential settings

These are the only fields you need to get started.

| Field | Description | Default |
|---|---|---|
| `nibe_host` | IP address of your controller | `192.168.2.201` |
| `nibe_username` | Local API username | — |
| `nibe_password` | Local API password | — |
| `device_name` | How the controller appears in HA. **Set a distinct, location-specific name if you run multiple controllers** — e.g. `"Nibe Garage"` or `"Heat Pump North"`. This becomes the prefix on every entity name. Changing it after initial discovery orphans the old entities. | `Nibe SMO S40` |

### All settings

| Field | Description | Default |
|---|---|---|
| `nibe_host` | IP address of your controller | `192.168.2.201` |
| `nibe_port` | HTTPS port of the local REST API | `8443` |
| `nibe_username` | Local API username | — |
| `nibe_password` | Local API password | — |
| `mqtt_host` | MQTT broker hostname | `core-mosquitto` |
| `mqtt_port` | Broker port — use `1883` for plaintext (default) or `8883` for TLS | `1883` |
| `mqtt_username` | MQTT username (leave blank if no authentication) | — |
| `mqtt_password` | MQTT password (leave blank if no authentication) | — |
| `device_name` | How the controller appears in HA | `Nibe SMO S40` |
| `poll_interval` | Fetch interval in seconds. Recommended: `15`, `30`, `60`, `120`, or `300`. Other values are snapped to the nearest. | `30` |
| `mode` | Which entities the bridge exposes at startup. See [Entity Modes](#entity-modes). | `essential` |
| `log_level` | Log verbosity. `debug` also unlocks diagnostic entities on the Management device. Restart required. | `info` |
| `api_failure_threshold` | Consecutive failed polls before an HA notification appears | `3` |
| `changelog_retention_days` | Days to retain Entity Manager changelog entries. Minimum 50 entries always kept. | `90` |
| `remove_frontend` | Set to `true` before stopping the add-on to clean up Lovelace resources on shutdown. See [Uninstalling](#uninstalling). | `false` |
| `mqtt_tls` | Enable TLS for broker traffic. Requires broker to be configured for TLS. | `false` |
| `mqtt_ca_cert` | Path to PEM CA certificate for the MQTT broker. Only needed for self-signed broker certificates. Example: `/ssl/ca.pem` | — |
| `nibe_ca_cert` | Path to PEM CA certificate for the Nibe controller. Leave blank to accept the controller's built-in self-signed certificate. | — |

> ⚠️ **Poll interval** — the controller is an embedded device; its REST API is secondary to managing your heat pump. The default of 30 seconds is recommended. A restart is required after changing this setting.

### Persistent notifications

The bridge surfaces important events in the HA notification bell.

| Notification | Trigger | Clears automatically |
|---|---|---|
| **Active Alarm(s)** | One or more alarms on the controller | Yes — when all alarms clear |
| **API Unreachable** | `api_failure_threshold` consecutive fetch failures | Yes — on next successful fetch |
| **Write Failed** | A register write was rejected or timed out | Yes — on next successful write |
| **Started Without Device** | Controller unreachable at bridge startup | Yes — on first successful poll |
| **Dynamic entity disabled in HA** | User disabled a dynamic entity; bridge re-enabled it | No — replaced on each occurrence |
| **Entity disabled in HA** | User disabled a static entity via HA settings | No — replaced on each occurrence |
| **Nibe Menus — Dashboard updated** | Dynamic point appeared/disappeared in `menus` mode | No — dismissed manually |

Alarms are polled every 10 seconds, regardless of `poll_interval` — an alarm condition appears in HA within 10 seconds of occurring.

### Credentials via secrets.yaml

As an alternative to entering credentials in the add-on UI, you can supply them via `secrets.yaml` — useful if you share your HA configuration in a repository.

**Nibe API credentials** — supply a pre-encoded Basic auth token:

```yaml
# secrets.yaml
nibe_basic_auth: "dXNlcjpwYXNzd29yZA=="   # base64 of "user:password"
```

Generate the token:
```
echo -n "username:password" | base64
```

On Windows (PowerShell):
```
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("username:password"))
```

**MQTT credentials:**
```yaml
# secrets.yaml
mqtt_user: "my_mqtt_user"
mqtt_password: "my_mqtt_password"
```

The bridge checks `/config/secrets.yaml` and `/homeassistant/secrets.yaml` automatically. Values found here take priority over the add-on configuration UI.

> ℹ️ Passwords containing special characters including `#` are supported when the value is quoted in `secrets.yaml`.

### Changing credentials

Credential changes — whether in the add-on UI or in `secrets.yaml` — take effect on the next add-on restart. There is no live re-authentication flow. If the bridge is running and credentials change on the controller side, the bridge will log an authentication error and stop fetching data. Restart the add-on after updating the credentials in the configuration.

### TLS configuration

By default both connections — to the Nibe controller and to the MQTT broker — use unverified TLS or plaintext. This is acceptable on a trusted home network. If you want to strengthen either connection:

**MQTT broker TLS** — encrypts all traffic between the bridge and the broker.

Before enabling `mqtt_tls`, configure Mosquitto for TLS by adding to its configuration:
```yaml
certfile: fullchain.pem
keyfile: privkey.pem
```
These are the same certificate files used by the HA frontend. Then in the bridge:
1. Set `mqtt_tls` to `true`
2. Change `mqtt_port` to `8883`
3. Leave `mqtt_ca_cert` blank for Let's Encrypt or publicly-signed certificates
4. Set `mqtt_ca_cert` only for self-signed broker certificates

> ⚠️ Port 8883 will refuse connections until Mosquitto is configured for TLS.

**Nibe controller TLS** — the controller uses a self-signed certificate. The bridge accepts it without verification by default, which is correct for all standard installations. Leave `nibe_ca_cert` blank unless you have installed a custom CA certificate on the controller through other means.

---

## How Data Gets to HA

### Polling

The bridge fetches all data points from the Nibe controller in a single bulk HTTP request on every poll cycle. The default interval is 30 seconds — every entity updates at most once per cycle.

This means:
- **The value you see in HA is always up to one poll interval old.** If the compressor starts, it will appear in HA within 30 seconds (default), not instantly.
- **There is no push notification from the controller.** The bridge polls; the controller does not call home.
- **Alarms are an exception** — the bridge polls the alarm endpoint every 10 seconds regardless of `poll_interval`, so an alarm appears in HA within 10 seconds of occurring.

Use the **Force Poll** button on the Management device to trigger an immediate fetch after making a change on the controller itself.

### MQTT retained messages

All entity discovery configs and state values are published to the MQTT broker as **retained messages**. This means:
- When HA restarts, the MQTT integration reads all retained values from the broker immediately — entities appear with their last-known values before the first poll completes.
- When the bridge restarts, it reconnects and restores its entity list from the broker's retained messages rather than rediscovering from scratch.
- The broker acts as the bridge's state store. No data is persisted locally on the bridge itself.

### Startup window

After the bridge starts, there is a window of approximately 30 seconds before the first poll completes and all entity states are populated. During this window, newly registered entities may appear as `unavailable` in HA. This is normal — they will update on the first successful poll.

If entities remain `unavailable` for more than a minute, check the add-on log and the **API Reachable** binary sensor on the Management device.

### No historical backfill

The bridge only provides current values. If the bridge is stopped for any period — planned or unplanned — data from that window is not collected and cannot be recovered. For long-term data retention, the HA Recorder or an external database (InfluxDB, MariaDB) is recommended.

---

## Entity Modes

The `mode` option controls which data points are enabled as HA entities at startup. Changing mode requires an add-on restart — it disables entities not in the new set, including any extras you enabled manually via the Entity Manager card.

| Mode | Description |
|---|---|
| `essential` | Core temperatures, operating mode, energy overview — recommended starting point |
| `monitoring` | A broader view similar to the Nibe Uplink cloud service |
| `advanced` | Monitoring plus diagnostic and tuning registers (PV, ACS, manual modes) |
| `menus` | All points referenced in the Nibe controller's menu structure, plus a dedicated **Nibe Menus** dashboard that mirrors the controller's physical menu hierarchy — recommended for users familiar with the Nibe installer menus |
| `all` | Every available data point — see warning below |
| `none` | Nothing enabled — manage everything manually via the Entity Manager card |

> ⚠️ **`all` mode** can create over a thousand HA entities on a fully equipped installation. This will noticeably affect dashboard performance and database size. Use `advanced` instead — it covers almost every register a typical user would want.

The Management device's **Entity Mode** sensor confirms which mode is currently active.

---

## Entities in Home Assistant

After starting, two devices appear under **Settings → Devices & Services → MQTT**:

- **Your controller device** (named after your hardware — e.g. **SMO S40**, **VVM S320**) — all discovered sensors and controls
- **Management device** — controls for the bridge itself

### Entity types

The bridge creates the following HA entity types from the controller's data points:

| Type | Examples | Writable |
|---|---|---|
| `sensor` | Outdoor temperature, supply temperature, degree minutes, energy counters, compressor frequency | No |
| `binary_sensor` | Compressor running, pump active, alarm present, AUX relay state | No |
| `number` | Heating curve offset and slope, hot water setpoint, DM start/stop thresholds | Yes |
| `switch` | Operating mode overrides, auxiliary heat enable, holiday mode | Yes |
| `select` | Smart Mode (Normal/Away), heating system type, language | Yes |
| `button` | Alarm reset, compressor block | Yes (trigger-only) |

On a fully equipped S-series installation the bridge typically creates 900–1,200 entities. The `essential` mode enables around 30 of the most useful ones at startup; the rest are available via the Entity Manager card.

Entities fall into two categories:
- **Diagnostic** — read-only sensors: temperatures, pressures, flow rates, operating states
- **Config** — writable settings: setpoints, operating modes, schedules

> ⚠️ **Config entities write directly to the controller.** Changes in HA are sent to the controller immediately. If you are exploring unfamiliar registers, observe their current values before writing. The Entity Manager card shows a **Writable** badge on any entity that accepts commands.

> ℹ️ Entity names are always in English — the REST API is English-only. The language setting on the controller affects its own menus, not the HA entity names.

### Dynamic data points

Some data points only appear when a related feature is active. These **dynamic data points** are added and removed automatically as the controller's state changes.

![Dynamic points lifecycle](/local/nibe-dynamic-points.svg)

**First-time discovery** — when you flip a switch for the first time, the bridge runs a one-time learning scan:
- It watches for new data points to appear for up to 90 seconds after your write
- If new points appear, the relationship is recorded permanently — future flips open the same scan window
- If no new points appear, the switch is recorded as non-controlling and future writes execute immediately

**After learning** — the bridge immediately opens the 90-second scan window when you flip a known controlling switch. Dynamic entities appear within the next bulk poll cycle (up to ~60s). Disappearances are typically faster (~16s observed on S2125).

**Firmware updates** — if a firmware update changes the relationship between a switch and its dynamic points, the bridge detects this automatically on the next flip. The stale relationship is cleared, a `firmware changed` entry appears in the Changelog, and the next flip triggers a new learning scan.

> ⚠️ **Disabling a dynamic entity via HA (cog icon) is not supported.** Dynamic points exist because the firmware exposes them — their lifecycle is firmware-controlled. The bridge re-enables any dynamic entity disabled via HA. To permanently stop seeing a dynamic entity, change the value of the register that controls it.

> ℹ️ When two switches are flipped in quick succession, the Changelog's **Triggered by** field shows the most recent write at the time of detection — this attribution is approximate. The point appearances and disappearances themselves are always detected correctly.

Use the **Flush Dynamic Map (DEBUG)** button to clear all learned relationships and start fresh. Only visible when `log_level` is `debug`.

### Management entities

| Entity | Type | Category | Description |
|---|---|---|---|
| Entity Mode | Sensor | Diagnostic | Currently applied entity mode — read-only |
| Smart Mode | Select | Config | Switch between Normal and Away mode |
| Aid Mode | Switch | Config | Auxiliary-heat-only mode — readable and writable |
| Reset Alarms | Button | Config | Clear all active alarms on the controller |
| Force Poll | Button | Config | Trigger an immediate data fetch |
| Regenerate Dashboard | Button | Config | Rebuild the Nibe Menus dashboard (only in `menus` mode) |
| Mark Changes Read | Button | Config | Reset the Entity Manager changelog unread badge |
| Entity Stats | Sensor | Diagnostic | Total, enabled, and active entity counts |
| Active Alarms | Sensor | Diagnostic | Number of active alarms; attributes contain full alarm detail |
| API Reachable | Binary sensor | Diagnostic | ON when the last bulk fetch succeeded |
| Bridge Uptime | Sensor | Diagnostic | Seconds since the bridge started |
| API Last Fetch | Sensor | Diagnostic | Timestamp of the last successful bulk data fetch |
| API Fetch Duration | Sensor | Diagnostic | Duration of the last bulk fetch in seconds |

The following entities are only visible when `log_level` is `debug`:

| Entity | Type | Description |
|---|---|---|
| Flush Dynamic Map | Button | Clear all learned dynamic point relationships |
| Run Test Suite | Button | Trigger the built-in test suite |
| Test Suite Result | Sensor | Last test suite outcome — full detail in the Attributes tab |

> 💡 **Force Poll** is useful after making a change on the controller — it updates HA immediately rather than waiting for the next scheduled poll.

> 💡 **API Reachable** and **Bridge Uptime** can be used in automations to alert you if the bridge loses contact with the controller while still running.

### Entity attributes

Every entity exposes static attributes from the firmware metadata, visible in the entity detail panel and accessible in templates and automations.

| Attribute | Description |
|---|---|
| `default_value` | Factory default value in display units — e.g. `20 °C`. Useful for restoring after experimentation. |
| `point_id` | The numeric register ID used internally by the bridge and the REST API. |
| `modbus_register` | The Modbus register address — for cross-referencing with installer documentation. |
| `writable` | `true` if the register accepts write commands. |
| `description` | The firmware's own description string, where provided. Sometimes contains enum value meanings. |

Attributes are published once when an entity is enabled and do not change between polls.

```yaml
# Example: show deviation from factory default
{{ states('number.nibe_smo_s40_room_sensor_setpoint') }} vs default {{ state_attr('number.nibe_smo_s40_room_sensor_setpoint', 'default_value') }}
```

### Aid Mode

Aid Mode means the compressor is off and heating comes entirely from an electric auxiliary source. The controller activates it automatically on fault conditions, or an installer can set it manually.

> ⚠️ **A heat pump delivers roughly 3–5× more heat per unit of electricity than a resistive heater. Running in Aid Mode unexpectedly will significantly increase your energy bill.**

Aid Mode is readable and writable — you can turn it off from HA if an installer set it manually and forgot to clear it.

> 💡 **Create an automation** to alert you when Aid Mode activates: Settings → Automations → New → Trigger: State → Entity: Aid Mode → To: On → Action: Notify → "Heat pump is running in Aid Mode — check for faults"

### Energy dashboard

The bridge exposes energy and power data points compatible with HA's built-in Energy dashboard under **Settings → Dashboard → Energy**.

> 💡 Search for `kWh` in the Entity Manager card to find all energy registers on your installation. Lifetime accumulator registers (`total_increasing`) are suitable for energy dashboard tracking; instantaneous power registers (`measurement`) for power monitoring.

---

## Using the Bridge in Automations

All entities created by the bridge are standard HA entities — they work in automations, scripts, templates, and the Energy dashboard exactly like any other HA entity.

### Entities as triggers

Any sensor or binary_sensor can trigger an automation. Common trigger patterns:

**Alert when Aid Mode activates:**
```yaml
trigger:
  - platform: state
    entity_id: switch.nibe_smo_s40_aid_mode
    to: "on"
action:
  - action: notify.persistent_notification
    data:
      message: "Heat pump switched to Aid Mode — compressor is off. Check for faults."
      title: "Heat pump alert"
```

**Alert when the bridge loses contact with the controller:**
```yaml
trigger:
  - platform: state
    entity_id: binary_sensor.nibe_smo_s40_api_reachable
    to: "off"
    for:
      minutes: 5
action:
  - action: notify.persistent_notification
    data:
      message: "Nibe bridge cannot reach the controller. Check network and controller."
      title: "Heat pump unreachable"
```

**Adjust hot water temperature at night:**
```yaml
trigger:
  - platform: time
    at: "22:00:00"
action:
  - action: number.set_value
    target:
      entity_id: number.nibe_smo_s40_hot_water_comfort_mode_start_temperature
    data:
      value: 45
```

### Write commands as actions

Writable entities (marked with the **Writable** badge in the Entity Manager card) accept standard HA service calls:

| Entity type | Service | Notes |
|---|---|---|
| `switch` | `switch.turn_on`, `switch.turn_off`, `switch.toggle` | Aid Mode, operating mode overrides |
| `number` | `number.set_value` | Setpoints, temperature targets, curve settings |
| `select` | `select.select_option` | Operating mode, heating system type |
| `button` | `button.press` | Alarm reset, force poll, dashboard regen |

> ⚠️ Write commands go directly to the controller. The bridge confirms the write was accepted by the firmware but does not verify the controller acted on it — some values are overridden by the firmware's own logic. See [Firmware-controlled values](#firmware-controlled-values--what-you-can-and-cannot-change).

### Switching to Away mode when you leave

```yaml
trigger:
  - platform: zone
    entity_id: person.your_name
    zone: zone.home
    event: leave
action:
  - action: select.select_option
    target:
      entity_id: select.nibe_smo_s40_smart_mode
    data:
      option: Away
```

### Reading entity attributes in templates

Every entity exposes `point_id`, `modbus_register`, `writable`, and `default_value` as attributes. These are accessible in templates:

```yaml
# Show current heating curve offset vs factory default
{{ states('number.nibe_curve_offset') }} 
(default: {{ state_attr('number.nibe_curve_offset', 'default_value') }})
```

---

## Entity Manager Card

The bridge exposes over a thousand data points. The companion Lovelace card is the practical way to manage which are enabled — without it, the default HA device page is impractical at this scale.

On every startup the bridge automatically copies the card file, registers it as a Lovelace resource, and creates the **Nibe Bridge** dashboard. The dashboard is created once and never overwritten — it is yours to customise. In `menus` mode, a **Nibe Menus** dashboard is also created and rebuilt on every startup to reflect your current enabled entities.

> ℹ️ The card file in `/config/www/` is replaced on every add-on start. Do not edit it directly.

If you manually delete the Nibe Bridge dashboard, restart the add-on — it recreates missing dashboards automatically.

### Features

- **Search** — fuzzy search across all point names as you type; also searches by unit (`°C`, `kWh`, `Hz`, `%`), point ID, and Modbus register number. Best matches appear first. Queries under 3 characters use exact matching; longer queries are typo-tolerant (typing `"temprature"` still finds temperature sensors). Requires a network connection on first load to fetch the Fuse.js search library from cdnjs; falls back to exact substring matching if unavailable.
- **Filters** — by type, status (enabled/disabled), and access (writable/read-only)
- **Sorting** — click any column header; click again to reverse
- **Enable / disable** — individually or in bulk via checkboxes
- **Entity details** — full metadata including Modbus register, unit, value range, divisor, factory default, and last metadata update time
- **Writable badge** — entities that accept write commands are marked; changes go directly to the controller
- **Changelog** — badge shows unread dynamic entity events; each entry identifies the controlling register that caused the change
- **Mobile layout** — card-based view with larger touch targets on small screens

### Nibe Menus dashboard

When `mode` is set to `menus`, the bridge creates a second dashboard — **Nibe Menus** — that mirrors the physical menu structure of the Nibe controller. The same hierarchy an installer navigates on the controller touchscreen appears as HA dashboard views: 163 menus, organised exactly as Nibe structures them.

This makes `menus` mode the natural choice for users who already know the Nibe controller interface and want that same structure in HA — every setting is where you would expect it to be.

A few things to know about the Nibe Menus dashboard:

- **It is rebuilt on every startup** — unlike the Nibe Bridge dashboard which is created once and left alone, the Nibe Menus dashboard is regenerated each time the add-on starts to reflect which entities you currently have enabled. Do not customise its layout — changes will be overwritten on the next restart.
- **Dynamic points slot in automatically** — when a controlling switch exposes new registers, they appear in the correct menu location in the dashboard. Reload the dashboard in your browser when you see the "Nibe Menus — Dashboard updated" notification.
- **Use Regenerate Dashboard** on the Management device to rebuild it manually without restarting the add-on — useful after enabling or disabling a batch of entities.
- **Switching away from `menus` mode** removes the Nibe Menus dashboard automatically on the next restart.



To add the card to an existing dashboard:

```yaml
type: custom:nibe-entity-manager-card
```

Optional configuration:

```yaml
type: custom:nibe-entity-manager-card
title: "My Heat Pump"         # optional; default: no header
pageSize: 250                 # optional; default: 50
suppressInitialToasts: true   # optional; default: true
```

**`title`** — shown in the card header. Leave empty when using a panel view where the view title already identifies the content.

**`pageSize`** — length of the data points table. Larger values mean fewer page turns when browsing.

**`suppressInitialToasts`** — suppresses toasts shown when the card first loads. With the default of `true`, only real-time dynamic entity changes produce toasts.

---

## Snapshots

A snapshot saves your current set of enabled entities under a name. Restore it later with one click to get back exactly that selection — useful for switching between profiles, recovering from an accidental mode change, or sharing a curated set with someone on the same hardware.

### Saving a snapshot

Open the Entity Manager card and click **Snapshots** in the toolbar. Enter a name and click **Save**. The bridge stores the current enabled point IDs, the timestamp, and the applied mode at save time. Up to 10 snapshots can be saved; saving with an existing name replaces that snapshot.

### Restoring a snapshot

Click **Restore** next to a snapshot, then choose how to apply it:

| Option | What it does |
|---|---|
| **Replace current selection** | Disables everything currently enabled, then enables exactly the saved points — a clean slate |
| **Add to current selection** | Keeps everything currently enabled and additionally enables the saved points |

> ⚠️ **Restore is not available in `menus` or `all` mode.** Both modes manage the entity selection automatically — the mode re-applies on every restart and would overwrite any restored selection. Switch to `essential`, `monitoring`, `advanced`, or `none` first.

Dynamic points (firmware-controlled entities) are always protected during a flush restore — they are never disabled regardless of what the snapshot contains.

If a snapshot was saved on a different firmware version and some point IDs no longer exist, those points are silently skipped. The restore message tells you how many were skipped.

### Deleting a snapshot

Click **Delete** next to a snapshot and confirm. Deleted snapshots cannot be recovered.

### Persistence

Snapshots are stored in `/data/snapshots.json` inside the add-on's data volume. They survive add-on restarts, firmware updates, and MQTT broker restarts. They are included in HA backups. They are removed only if the add-on is uninstalled with **Remove frontend** enabled or if you delete them manually.

---

## Understanding Your Heat Pump

This section explains concepts that help you use the bridge and the Nibe Menus dashboard effectively. Nibe's documentation covers these topics partially at best.

### Degree minutes — how the controller decides when to heat

The controller does not use a simple on/off thermostat. Instead it tracks **degree minutes (DM)** — a running integral of how far the indoor temperature is below the target, accumulated over time. One degree minute means the temperature has been 1°C below target for one minute.

When degree minutes reach a negative threshold (typically −60 DM), the compressor starts. When they reach a positive stop threshold, it stops. This produces longer, more efficient compressor runs rather than rapid cycling.

The DM counter is visible in HA and is one of the most useful values for understanding why the heat pump is or is not running.

### Heating curve — slope versus offset

The heating curve determines the **supply temperature** — how hot the water going into the heating system should be — as a function of outdoor temperature.

**Offset** — shifts the entire curve up or down. If the house is consistently too cold or too warm by the same amount at all outdoor temperatures, adjust the offset only. This is the most common adjustment.

**Slope** — changes how aggressively supply temperature rises as outdoor temperature falls. Only adjust the slope if the house is comfortable at moderate outdoor temperatures but consistently too cold in very cold weather (or too warm in mild weather). The offset and slope interact: changing the slope requires rechecking the offset.

> ⚠️ **Room sensor changes the meaning of the temperature setting.** If a room sensor is installed, menu 1.1 shows a desired room temperature in °C. Without a room sensor it shows a curve offset from −10 to +10. Installing or removing a room sensor changes the scale and interpretation of this setting — always verify the value after any room sensor hardware change.

### Auto mode and the averaging paradox

The controller uses an **averaged** outdoor temperature (filtered over several hours) to decide when to switch between heating and cooling seasons — not the current instantaneous reading. This prevents the system from switching modes on a single warm afternoon only to switch back that evening.

The practical effect is counterintuitive: on a warm spring day with 20°C outside, the system may still be in heating mode because the 24-hour average is still below the heating stop threshold. This is not a fault. The filtering time is configurable in menu 7.1.10.2.

### Firmware-controlled values — what you can and cannot change

Not all writable registers behave like typical HA controls. The firmware actively manages many registers as part of its automatic control logic.

**Volatile values** — the firmware recalculates these continuously. Writing has no lasting effect. Examples: degree minute counters, calculated supply temperatures, compressor operating state. These are sensors for monitoring, not for control.

**Competed settings** — the firmware also writes to these as part of its auto mode logic, but user writes do take effect and persist until the firmware overrides them. Examples: operating mode, heating/cooling stop temperatures, DM thresholds.

**Persistent settings** — the firmware reads these once and uses them as parameters. User writes are fully respected. Examples: heating curve slope and offset, supply temperature limits, pump speeds. These are the true settings — changing them has a lasting effect.

### SPA and SG Ready

Both **Smart Price Adaption (SPA)** and **SG Ready** can simultaneously influence the heating setpoint. If both are active and give conflicting signals, the controller resolves the conflict according to its internal priority — SG Ready typically takes precedence as it is a direct grid signal, but this is not guaranteed across all firmware versions. Test their interaction on your installation before relying on either for critical demand response.

### Emergency mode and electricity bills

Emergency mode (Aid Mode) runs on electric auxiliary heat only — the compressor is off. A heat pump in normal operation delivers roughly 3–5 kWh of heat per kWh of electricity. In emergency mode that drops to 1:1. If the compressor develops a fault and the controller falls back silently to emergency mode, electricity consumption can triple or quadruple.

The bridge surfaces this as a persistent HA notification and via the Aid Mode switch entity. Create an automation to alert you immediately when Aid Mode activates.

---

## Intentionally Unexposed Registers

Several registers that appear in the Nibe firmware are deliberately not exposed as controllable entities. This section explains which ones and why.

### The REST API / Modbus TCP split

Nibe divides register access into two tiers. The local REST API (which this bridge uses) exposes settings safe for remote, asynchronous access. Modbus TCP gives direct register-level access with no safety envelope. Some registers appear with `isWritable: false` in the REST API even though they are physically writable over Modbus — Nibe marks them read-only intentionally. The bridge respects this boundary.

### Register 55884 — Set point value power (Modbus 5997)

Accepts a direct compressor power request in kW, bypassing the normal degree-minute algorithm. There is no firmware-side timeout — a value written here persists indefinitely, including across power cycles, until explicitly cleared. If the bridge crashes while a non-zero value is active, the compressor runs at that commanded level with no automatic recovery. Safe only within a system that implements its own watchdog process.

### Modbus TCP sensor injection — registers 5217–6006

Allow injection of synthetic sensor values — outdoor temperature, room temperature, and others — directly into the firmware's sensor inputs. The firmware treats injected values identically to physical sensors and does not distinguish between them. Injected values persist across power cycles with no timeout. A stale injected outdoor temperature of −10°C would cause the controller to run at high heating demand regardless of actual conditions.

### Spot price registers 26817–26840

24 registers for hourly electricity price signals intended for local injection of spot price data. The write path is Modbus TCP only. Nibe does not document the expected value format in any publicly available manual — the exact scaling and interaction with the controller's SPA logic are unconfirmed.

### Register 55749 — Block new compressor (Modbus 5551)

Appears in the firmware as a boolean holding register but is marked `isWritable: false` in the REST API. Only becomes responsive to external writes during an active system event. Without the ability to observe its behaviour during a real block event, there is no basis for writing a correct integration.

---

## Known Limitations

These are design constraints, not bugs. Understanding them helps set the right expectations.

**Poll lag** — entity values update on each poll cycle, not in real time. The default 30-second interval means any change on the controller appears in HA up to 30 seconds later. Reducing `poll_interval` reduces the lag at the cost of more API load on the controller — the controller is an embedded device and its REST API is secondary to its core function.

**No historical backfill** — if the bridge is offline for any period, data from that window is not collected. There is no mechanism to retrieve historical values from the controller.

**Dynamic entities re-enable themselves** — dynamic data points are firmware-controlled. If you disable a dynamic entity via the HA settings cog, the bridge detects this and re-enables it, because the firmware continues to report that point. To stop a dynamic entity from appearing, change the register value that controls it (e.g. switch the controlling switch off). See [Dynamic data points](#dynamic-data-points).

**One controller per bridge instance** — the bridge connects to one Nibe controller. To monitor two or more controllers, install the add-on multiple times — one instance per controller. Each instance connects to a different IP address and runs independently.

> ⚠️ **Choose distinct `device_name` values before starting.** The bridge derives a unique internal identifier from the controller's serial number, so there is no risk of MQTT topic collision between instances. However, all Nibe controllers expose the same ~1,200 data points with identical names — "Outdoor temperature", "Supply temperature", "Degree minutes", and so on. If you leave both instances at the default device name, HA will suffix the second instance's entities with `_2`, `_3`, and so on to avoid collisions. The result is two sets of identically-named entities that are impossible to tell apart at a glance.
>
> Use names that reflect the physical installation — `"Nibe Garage"` and `"Nibe Main House"`, or `"Heat Pump North"` and `"Heat Pump South"`. The device name becomes the prefix on every entity name, so `"Nibe Garage"` produces `sensor.nibe_garage_outdoor_temperature`, which is immediately clear. Set this before the first start — changing `device_name` after initial discovery orphans the old entities in HA and creates new ones under the new name.

**Write confirmation is not state verification** — when you write a value, the bridge confirms the firmware accepted the write, but does not verify the controller's actual state changed. Some registers are recalculated by the firmware's own control logic and may not hold the written value. See [Firmware-controlled values](#firmware-controlled-values--what-you-can-and-cannot-change).

**No write rate limiting** — the bridge does not throttle write commands. Rapidly writing to the same register in an automation loop is technically possible but not recommended — the controller may queue or reject rapid writes.

**Restart required for configuration changes** — changing `nibe_host`, credentials, `poll_interval`, `mode`, or `log_level` requires an add-on restart. There is no live reconfiguration path.

**English entity names only** — the Nibe REST API provides entity names in English regardless of the language setting on the controller itself. Entity names cannot currently be translated.

---

## Troubleshooting

**No entities appear after starting**
The MQTT integration must be configured in HA (Settings → Devices & Services → Add Integration → MQTT) and connected to the same broker as this add-on.

**Cannot connect to Nibe API**
- Verify `nibe_host` is the correct IP address of your controller
- Confirm the local API is enabled in Menu 7.5.15 on the controller
- Test reachability: open `https://<nibe_host>:8443` in a browser — a certificate warning is normal

**Credentials error on startup**
`nibe_username` and `nibe_password` must match the local API settings exactly. Passwords are case-sensitive.

**Cannot connect to MQTT broker**
- The Mosquitto add-on must be installed and running before this add-on starts
- `core-mosquitto` only works with the official Mosquitto add-on — use an IP address or hostname for other brokers
- If authentication is enabled on the broker, fill in `mqtt_username` and `mqtt_password`

**Entities unavailable after restart**
Normal for the first 30 seconds while the bridge fetches current values. If still unavailable after a minute, check the add-on log for errors.

**Aid Mode on but heat pump appears to be working**
An installer may have set it manually. Turn it off directly from the **Aid Mode** switch on the Management device, or check the controller itself.

**API Reachable shows OFF**
The bridge is running but cannot reach the Nibe REST API. Check that the controller is reachable (`https://<nibe_host>:8443` in a browser) and has not rebooted and changed its IP. Assigning a static IP to the controller is recommended. The bridge recovers automatically once the API responds.

**Active Alarms is non-zero**
A persistent notification has appeared in the HA notification bell with full alarm detail. Use the **Reset Alarms** button to clear alarms once the underlying issue is resolved. The notification dismisses automatically when all alarms clear.

**Entity Manager card not appearing**
Try reloading browser resources: **Settings → Dashboard → ⠇ → Reload resources**. If the dashboard itself is missing, restart the add-on — it recreates missing dashboards automatically.

**A number entity shows an empty state or cannot be written**
The firmware occasionally stores register values outside the range it also reports. The bridge detects this, logs a warning containing `outside firmware range`, and adjusts the discovery config so HA can display the value correctly. No action required.

**A dynamic point keeps reappearing after I disable it in HA**
By design — dynamic points are firmware-controlled. The bridge re-enables any dynamic entity disabled via HA. To permanently stop seeing a dynamic entity, change the value of the register that controls it. The Changelog identifies which register write triggered each dynamic point to appear.

**I disabled a static entity via the HA cog but the bridge still polls it**
When you disable a static entity via Settings → Devices → entity cog → Disable, the bridge detects this in real time and removes the entity from its active list. Use the Entity Manager card to re-enable it.

---

## Deployment Notes

### Requirements

This add-on requires the Home Assistant add-on environment (HA OS or HA Supervised). It is not designed for standalone Docker or HA Core/Container installations.

### Restart behaviour

**HA Core restart** — add-ons keep running. The bridge keeps polling, MQTT messages keep flowing, and when HA Core comes back up it reads all retained discovery configs and state topics from the broker. Entities don't go unavailable — they show the last retained value until the next poll.

**Full supervisor restart or host reboot** — add-ons are stopped and restarted in dependency order. There is a window where the bridge is down; data from that period is not collected. The bridge has no local cache — it relies on the broker as its state store.

**Add-on update** — stops and restarts just that add-on. During the restart window (~5–30 seconds) no polling happens. On restart the bridge reconnects, restores its entity list from the broker's retained messages, and resumes polling.

### The SUPERVISOR_TOKEN

The HA Supervisor automatically injects a `SUPERVISOR_TOKEN` into every running add-on. The bridge uses it for Lovelace setup, persistent notifications, and entity registry sync. If absent, those features are skipped — everything else continues normally.

| Feature | Without token |
|---|---|
| Heat pump data in HA | ✅ Full |
| Write commands | ✅ Full |
| Entity Manager card | ✅ Full |
| Bridge health diagnostics | ✅ Full |
| Lovelace dashboard | ❌ Not provisioned — add the card manually |
| HA notification bell alerts | ❌ Not sent — check add-on log instead |
| HA settings enable/disable sync | ❌ Not real-time — use the card |

In a normal HA OS or Supervised installation you will never see `No SUPERVISOR_TOKEN` in the log. If you do, restarting the Supervisor or the host usually resolves it.

### Configuration file locations

| Path | Purpose |
|---|---|
| `/data/options.json` | Add-on configuration (written by the HA UI) |
| `/config/secrets.yaml` or `/homeassistant/secrets.yaml` | Optional credential override |
| `/config/www/` | Destination for the Lovelace card file |

There is no separate config file option — `/data/options.json` is written automatically by the HA add-on UI. Environment variables are not used for configuration (other than `SUPERVISOR_TOKEN`, injected automatically by the Supervisor).

### Uninstalling

Removing the add-on through the HA Supervisor — even with **Remove add-on data** checked — does not automatically clean up items the bridge created outside its data volume.

| Item | Location |
|---|---|
| Card file | `/config/www/nibe-entity-manager-card.js` |
| Lovelace resource | Settings → Dashboards → ⠇ → Resources |
| Nibe Bridge dashboard | HA sidebar → Nibe Bridge |
| Nibe Menus dashboard | HA sidebar → Nibe Menus |

**Option A — Automatic cleanup (recommended)**

1. Go to **Settings → Add-ons → Nibe S-Series MQTT Bridge → Configuration**
2. Set `remove_frontend` to `true` and save
3. **Stop** the add-on cleanly — do not force-kill it. Cleanup runs during the normal shutdown sequence.
4. Remove the add-on from the Add-on Store

The bridge also clears all its MQTT retained messages at shutdown — every topic under `homeassistant/*/nibe_*/*` and `nibe/browser/#`.

> ⚠️ `remove_frontend` is only evaluated at shutdown. Normal restarts and updates are not affected.

**Option B — Manual cleanup**

If you have already removed the add-on without running the automatic cleanup:

1. **Card file** — delete `/config/www/nibe-entity-manager-card.js` via the HA file editor or SSH
2. **Lovelace resource** — Settings → Dashboards → ⠇ → Resources → find `nibe-entity-manager-card.js` → delete
3. **Nibe Bridge dashboard** — Settings → Dashboards → Nibe Bridge → ⠇ → Delete
4. **Nibe Menus dashboard** — Settings → Dashboards → Nibe Menus → ⠇ → Delete

To clear retained MQTT messages manually, use an MQTT client such as [MQTT Explorer](https://mqtt-explorer.com/) — connect to your broker and delete the entries under the `homeassistant` and `nibe` trees. Avoid wiping the entire broker persistence file as that removes messages from all other integrations.
