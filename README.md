# Home Assistant App: Nibe S-Series MQTT Bridge

![Supports aarch64 Architecture](https://img.shields.io/badge/aarch64-yes-green.svg)
![Supports amd64 Architecture](https://img.shields.io/badge/amd64-yes-green.svg)
[![GitHub Release][releases-shield]][releases]
[![License][license-shield]](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/blob/main/LICENSE.md)
[![Project Stage][project-stage-shield]](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge)

Connects your Nibe S-series heat pump controller to Home Assistant via MQTT — no cloud account, no hardcoded register maps, no manual configuration of data points.

## About

Nibe's S-series is a software platform that runs across the entire product range — standalone controllers (SMO S40), integrated indoor units (VVM S320, VVM S325, VVM S500), ground/water heat pumps (S1155, S1255, S1156, S1256), and ventilation heat pumps (S735). They all expose the same local REST API. This app uses that API to create native HA entities for everything the controller reports.

The bridge creates two devices in Home Assistant under **Settings → Devices & Services → MQTT**:

- **Your controller device** — all heat pump sensors, setpoints, and controls
- **Management device** — bridge controls: Smart Mode, Aid Mode, alarm monitoring, and diagnostics

> ⚠️ Writable entities send commands directly to the heat pump controller. Treat unfamiliar registers with care.

## What you get

On a typical installation the bridge creates 900–1,200 entities. A representative sample of what appears immediately in `essential` mode:

| What you see in HA | Examples |
|---|---|
| Temperatures | Outdoor, supply, return, hot water (top/bottom), room (with THS-10 sensor) |
| Operating state | Compressor on/off, pump status, operating mode, degree minutes |
| Energy | Compressor energy input, heat energy output, auxiliary heater usage |
| Controls | Heating curve offset and slope, hot water setpoint, DM start/stop thresholds |
| Mode switches | Smart Mode (Normal/Away), Aid Mode, holiday function |
| Alarms | Active alarm count and detail — updated every 10 seconds |

Accessories connected to the controller's internal bus appear automatically — zone modules, ventilation units, PV sensors, room sensors, pool heating, solar thermal, and more. See [Compatible Hardware](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/blob/main/DOCS.md#compatible-hardware) and [Supported Accessories](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/blob/main/DOCS.md#supported-accessories) for the full list.

## Features

- **Automatic entity discovery** — the controller describes its own data points; no register maps to maintain
- **Dynamic data points** — entities appear and disappear automatically as features activate (e.g. manual override registers appear when you switch to manual mode)
- **Nibe Menus dashboard** — a Lovelace dashboard mirroring the full Nibe installer menu hierarchy, rebuilt automatically on every startup
- **Entity Manager card** — a companion Lovelace card with search, filtering, sorting, enable/disable, and full firmware metadata per entity; automatically installed and provisioned on first start
- **Bidirectional control** — read sensor values and write settings back to the controller
- **Localized entity names** — translates entity titles/descriptions via the controller's own REST API (`language` setting, a dropdown of every language the controller supports); auto-detects Home Assistant's configured language by default
- **Mode-based entity management** — `essential`, `monitoring`, `advanced`, `menus`, `all`, or `none`; start small and expand as you explore
- **Active alarm monitoring** — faults appear in HA within 10 seconds via a dedicated fast poll
- **Persistent notifications** — active alarms, API outages, write failures, and dynamic point changes all surface in the HA notification bell
- **Management diagnostics** — API reachability, fetch duration, uptime, and entity counts visible as HA entities

## Compatible hardware

Works with any Nibe S-series controller running **minimum firmware 4.5.7** with the local REST API enabled (Menu 7.5.15). Full compatibility table in [Documentation](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/blob/main/DOCS.md#compatible-hardware).

**Not supported:** older Nibe controllers (SMO 20, SMO 40) and all F-series indoor units (VVM 225, VVM 310/320/325/500). Those systems use an ebus architecture with no REST API.

## Requirements

- Nibe S-series controller on your local network with the local REST API enabled
- [Mosquitto broker](https://github.com/home-assistant/addons/tree/master/mosquitto) app installed and running
- MQTT integration configured in Home Assistant (Settings → Devices & Services → Add Integration → MQTT)

## Installation

1. In Home Assistant go to **Settings → Apps → Install app → ⋮ → Repositories** and add:
   `https://github.com/whatsinabyte/nibe-smo-mqtt-bridge`
2. Install the **Nibe S-Series MQTT Bridge** app
3. Enable the local API on your controller — **Menu 7.5.15** on all S-series products
4. Configure the app with your controller's IP address and API credentials
5. Start the app

On first start the bridge automatically copies the companion card to `/config/www/`, registers it as a Lovelace resource, and creates the **Nibe Bridge** dashboard. Full installation instructions are in the [Documentation](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/blob/main/DOCS.md) tab.

## Good to know before installing

- **Values update every poll cycle** (default 30 seconds), not in real time. The controller does not push data — the bridge polls it.
- **If the bridge is offline, data from that window is not collected.** There is no backfill from the controller.
- **Configuration changes require a restart** — changing the IP address, credentials, poll interval, or mode takes effect on the next app start.
- **Changing the mode replaces your entity selection by default.** Switching from `essential` to `monitoring` enables additional entities; switching back disables them, including anything you enabled manually via the Entity Manager card. Set `mode_switch_behavior` to `merge` if you'd rather a mode change only ever add entities, never disable any. Use the Entity Manager card to manage individual entities within a mode.

## Links

- [Documentation](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/blob/main/DOCS.md)
- [Security policy](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/blob/main/SECURITY.md)

## Developer documentation

- [Architecture](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/blob/main/ARCHITECTURE.md) — system design, threading model, module reference
- [Contributing](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/blob/main/CONTRIBUTING.md) — dev environment, test suite, coding conventions
- [Card MQTT API](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/blob/main/docs/card-api.md) — MQTT protocol between bridge and Entity Manager card
- [Menu structure schema](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/blob/main/docs/menu-structure-schema.md) — how to add or edit menus in `menu_structure.yaml`

## License

MIT License — see [LICENSE](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/blob/main/LICENSE.md) for details.

[releases-shield]: https://img.shields.io/github/v/release/whatsinabyte/nibe-smo-mqtt-bridge.svg
[releases]: https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/releases
[license-shield]: https://img.shields.io/github/license/whatsinabyte/nibe-smo-mqtt-bridge.svg
[project-stage-shield]: https://img.shields.io/badge/project%20stage-experimental-yellow.svg
