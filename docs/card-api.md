# Entity Manager Card — MQTT API Reference

This document describes the MQTT protocol between the Nibe S-Series MQTT Bridge and the Entity Manager card (`nibe-entity-manager-card.js`). It covers every topic the card subscribes to, every topic it publishes to, and the JSON schema for each payload.

All topics in this document are fixed strings — none are per-entity or dynamically constructed. Per-entity HA discovery topics (`homeassistant/*/nibe_*/config`) are standard HA MQTT discovery and are not covered here.

For the source-level description of topic ownership and the publisher/handler modules, see [ARCHITECTURE.md — section 4.5](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/blob/main/ARCHITECTURE.md#45-nibe_mqtt_publisherpy--mqttdiscoverypublisher) (topic construction) and [section 4.7](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/blob/main/ARCHITECTURE.md#47-nibe_ha_integrationpy--ha-integration-layer) (management command handling).

---

## Topic namespaces

| Namespace | Purpose |
|---|---|
| `homeassistant/` | Standard HA MQTT discovery topics — entity configs, state, commands |
| `nibe/browser/` | Bridge-internal topics — card data, bridge health, management commands |

---

## Bridge → Card topics (subscribed by card)

These topics are published by the bridge and consumed by the card. All are retained unless noted.

---

### `nibe/browser/all_metadata`

**Retained.** Published once at startup. Contains metadata for every known firmware point in a single batched message. The card uses this to populate the entity list, detail panels, and search index.

The payload is plain JSON (not compressed).

```json
{
  "metadata": {
    "3945": {
      "id": 3945,
      "title": "Heating setpoint",
      "type": "number",
      "writable": true,
      "unit": "°C",
      "unit_overridden": false,
      "unit_raw": "°C",
      "min_value": 5,
      "max_value": 30,
      "category": "",
      "description": "Desired room temperature when room sensor is active.",
      "is_dynamic": false,
      "modbusRegisterID": 40123,
      "variableType": "INT_S16",
      "variableSize": 2,
      "modbusRegisterType": "MODBUS_HOLDING_REGISTER",
      "shortUnit": "°C",
      "divisor": 10,
      "decimal": 1,
      "change": 1
    }
  },
  "count": 1158,
  "last_updated": 1721825234.12
}
```

**Field notes:**
- `metadata` — object keyed by string point ID
- `unit_overridden` — `true` when `UNIT_OVERRIDES` replaced the firmware's reported unit (e.g. `%RH` → `%`)
- `unit_raw` — the original firmware unit before any override
- `divisor` — divide raw integer value by this to get the display value; `0` is treated as `1`
- `decimal` — number of decimal places to display
- `change` — minimum change in raw value before the firmware considers the value changed
- `modbusRegisterID` — Modbus TCP register address; `null` if not a Modbus point
- `is_dynamic` — `true` for points that only appear when a controlling switch is active

For individual point updates (after a dynamic point appears or disappears), a per-point message is published to `nibe/browser/meta/{point_id}` with the same schema as a single metadata entry (no outer `metadata`/`count` wrapper).

---

### `nibe/browser/point_list`

**Retained.** The authoritative list of all known point IDs. Published after initial discovery and after any dynamic change that adds or removes points. The card uses this to detect and remove stale entries without relying on per-point tombstones.

```json
{
  "points": [3, 4, 57, 599, 781, 832],
  "count": 1158,
  "last_updated": 1721825234.12
}
```

---

### `nibe/browser/enabled_state`

**Retained.** The current set of enabled point IDs. Published on startup and after every enable/disable operation. The card uses this to show which entities are active.

```json
{
  "enabled_points": [3945, 5079, 3671, 4],
  "count": 142,
  "timestamp": 1721825234.12
}
```

---

### `nibe/browser/dynamic_point_map`

**Retained.** The serialised `DynamicPointMap` — the causal table of controlling switch/select points and the dynamic points they expose. Published after every map update. The card displays this in the Dynamic Map view.

Payload is gzip-compressed JSON. Decompress before parsing.

```json
{
  "3754": {
    "point_id": 3754,
    "title": "Forced control",
    "entity_type": "switch",
    "processed_values": [0, 1],
    "unprocessed_values": [],
    "outcomes": {
      "1": {
        "appeared": [3755, 3756],
        "disappeared": []
      },
      "0": {
        "appeared": [],
        "disappeared": [3755, 3756]
      }
    }
  }
}
```

---

### `nibe/browser/active_dynamic_points`

**Retained.** The set of dynamic point IDs that are currently active (present in the firmware's bulk fetch response). Published after every bulk poll that detects a change.

```json
[3755, 3756]
```

Plain JSON array of integers.

---

### `nibe/browser/applied_mode`

**Retained.** The last-applied entity mode name. Plain string — not JSON.

```
essential
```

Valid values: `essential`, `monitoring`, `advanced`, `menus`, `all`, `none`.

---

### `nibe/browser/changelog/history`

**Retained.** The full changelog of dynamic point appearances and disappearances. Gzip-compressed JSON. The card decompresses and displays this in the Changelog panel.

```json
{
  "history": [
    {
      "timestamp": 1721825000.0,
      "iso_timestamp": "2025-07-24 14:03:20",
      "added": [3755, 3756],
      "removed": [],
      "id": "change_1721825000000",
      "unread": true,
      "source": "firmware",
      "triggered_by": 3754
    }
  ],
  "total_entries": 1,
  "unread_count": 1,
  "last_updated": 1721825234.12,
  "_seq": 5
}
```

**Field notes:**
- `added` / `removed` — arrays of point IDs that appeared or disappeared in this event
- `source` — always `"firmware"` (reserved for future use)
- `triggered_by` — point ID of the controlling switch/select that caused the change, or `null` if unknown
- `_seq` — monotonically increasing sequence number; the card uses this to skip stale retained messages after a map flush

---

### `nibe/browser/changelog/unread`

**Retained.** Lightweight unread count, published alongside `changelog/history` after every change or mark-read operation. The card uses this for the unread badge without decompressing the full history.

```json
{
  "unread_count": 3,
  "last_change": 1721825234.12
}
```

---

### `nibe/browser/snapshots`

**Retained.** The current list of saved snapshots. Published after every save, restore, or delete operation.

```json
[
  {
    "name": "Summer Profile",
    "timestamp": "2025-07-24 14:03:20",
    "point_ids": [3945, 5079, 3671],
    "point_count": 142,
    "mode": "essential"
  }
]
```

Maximum 10 snapshots. The array is ordered by creation time (most recently saved last).

---

### `nibe/browser/bridge/status`

**Retained.** Consolidated bridge health snapshot. Published on every poll cycle. Contains everything needed to assess bridge health without subscribing to multiple individual sensor topics.

```json
{
  "status": "healthy",
  "timestamp": 1721825234.12,
  "iso_timestamp": "2025-07-24 14:03:20",
  "uptime_s": 3600,
  "api": {
    "healthy": true,
    "consecutive_failures": 0,
    "failure_threshold": 3,
    "last_success": "2025-07-24 14:03:20",
    "last_fetch_duration_s": 0.412
  },
  "writes": {
    "total": 15,
    "success": 14,
    "failed": 1,
    "pending": 0,
    "success_rate_pct": 93.3,
    "last_error": "HTTP 500 on point 3945"
  },
  "entities": {
    "total_known": 1158,
    "mqtt_enabled": 142,
    "known_dynamic": 6
  }
}
```

**`status` values:** `"healthy"` or `"degraded"` (degraded when consecutive API failures ≥ failure threshold).

---

### `nibe/browser/bridge/alert`

**Not retained.** Published when an alertable condition is detected. Non-retained so automations only fire on the transition edge, not on every broker reconnect.

```json
{
  "alert_type": "api_unreachable",
  "severity": "error",
  "message": "API unreachable after 3 consecutive failures. Last success: 14:00:20.",
  "timestamp": 1721825234.12,
  "iso_timestamp": "2025-07-24 14:03:20",
  "context": {
    "failure_count": 3,
    "last_success": "2025-07-24 14:00:20"
  }
}
```

**`alert_type` values:**

| Value | Meaning |
|---|---|
| `api_unreachable` | Controller not responding |
| `api_restored` | Controller responding again after failures |
| `write_failed` | A write command to the controller failed |
| `write_restored` | Writes succeeding again after failures |
| `alarm_active` | One or more active alarms on the controller |
| `alarm_cleared` | All alarms cleared |

---

## Card → Bridge topics (published by card)

These topics are published by the card and consumed by the bridge. None are retained.

---

### `homeassistant/text/nibe_enable_entity/set`

Enable a single entity by point ID. Plain string payload — the integer point ID as a string.

```
3945
```

The bridge enables the entity, publishes its discovery config, and republishes `enabled_state`.

---

### `homeassistant/text/nibe_disable_entity/set`

Disable a single entity by point ID. Plain string payload — the integer point ID as a string.

```
3945
```

The bridge disables the entity, clears its discovery config, and republishes `enabled_state`.

---

### `nibe/browser/snapshots/cmd`

Snapshot commands. JSON payload.

**Save:**
```json
{"action": "save", "name": "Summer Profile"}
```

**Restore:**
```json
{"action": "restore", "name": "Summer Profile", "mode": "flush"}
```

```json
{"action": "restore", "name": "Summer Profile", "mode": "merge"}
```

- `flush` — disable all current entities, then enable the saved set
- `merge` — keep current entities and additionally enable the saved set
- Restore is blocked when the current mode is `menus` or `all` — the bridge logs a warning and takes no action

**Delete:**
```json
{"action": "delete", "name": "Summer Profile"}
```

---

## HA button press topics (card → bridge via HA)

These are standard HA button command topics. The card does not publish to them directly — the HA frontend sends `PRESS` when the user presses the corresponding HA button entity. They are listed here for completeness.

| Topic | Effect |
|---|---|
| `homeassistant/button/nibe_force_poll/press` | Immediately triggers a full bulk fetch from the controller |
| `homeassistant/button/nibe_regen_dashboard/press` | Rebuilds the Nibe Menus dashboard |
| `homeassistant/button/nibe_reset_alarms/press` | Clears all active controller alarms |
| `homeassistant/button/nibe_mark_changes_read/press` | Marks all changelog entries as read |
| `homeassistant/button/nibe_flush_dynamic_map/press` | Clears the dynamic point map (debug only) |
| `homeassistant/button/nibe_run_tests/press` | Runs the full pytest suite (debug only) |

---

## Compression

Two topics use gzip compression to reduce MQTT broker load:

| Topic | Compressed |
|---|---|
| `nibe/browser/all_metadata` | No — plain JSON |
| `nibe/browser/changelog/history` | Yes — gzip |
| `nibe/browser/dynamic_point_map` | Yes — gzip |

To decompress in JavaScript:
```javascript
const ds = new DecompressionStream('gzip');
const blob = new Blob([payload]);
const stream = blob.stream().pipeThrough(ds);
const text = await new Response(stream).text();
const data = JSON.parse(text);
```

---

## Availability

The bridge publishes its availability to:

```
homeassistant/sensor/nibe_bridge/available
```

Payload: `online` or `offline`. All management entity discovery configs reference this topic as their `availability_topic`. When the bridge is offline, all management entities show as unavailable in HA.
