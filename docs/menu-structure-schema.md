# menu_structure.yaml — Schema reference

`menu_structure.yaml` drives the entire **Nibe Menus** Lovelace dashboard. For a technical description of how the bridge reads and renders this file into Lovelace dashboard views, see [ARCHITECTURE.md — section 4.8](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/blob/main/ARCHITECTURE.md#48-nibe_lovelacepy--lovelace-provisioning). The bridge reads this file at startup and on every dashboard regeneration, building one dashboard view per top-level menu. Adding a menu, correcting a setting, or adding a tip or warning requires only editing this file — no Python changes needed.

---

## File structure

```yaml
menus:
  - id: '1'
    title: Indoor climate
    description: ...
    tip: ...
    submenus:
      - id: '1.1'
        title: Temperature
        settings:
          - label: Heating setpoint
            point_id: 3945
            range: 5 – 30 °C
            note: ...
```

The top level is a single `menus:` list. Each entry is a menu. Menus can contain `settings` (individual data point entries) and `submenus` (nested menus, rendered as sub-sections in the same dashboard view).

---

## Menu fields

| Field | Required | Type | Description |
|---|---|---|---|
| `id` | ✅ | string | Menu number as shown on the controller display (e.g. `'1'`, `'1.1'`, `'7.5.15'`). Always quote — YAML parses bare numbers like `1.1` as floats. |
| `title` | ✅ | string | Menu title as shown on the controller display. |
| `description` | ✅ | string | Explanatory text shown at the top of the menu section. Rendered as plain Markdown in the dashboard. Aim for 2–4 sentences covering what this menu does and when to use it. |
| `settings` | — | list | List of data point entries. Omit if the menu has no data points accessible via the local REST API. |
| `submenus` | — | list | Nested menus, rendered as sub-sections within the same dashboard view. |
| `tip` | — | string | Shown as a green callout box. Use for actionable guidance — what to do, in what order, how long to wait. |
| `note` | — | string | Shown as a blue callout box. Use for context, cross-references, and things the user should be aware of. |
| `warning` | — | string | Shown as a red callout box. Use for safety-critical information — condensation risk, electricity bills, system-wide overrides. |
| `local_api` | — | boolean | Set to `false` when the menu exists on the controller display but has no data points accessible via the local REST API (e.g. alarm logs, network settings, version history). This adds a "not accessible via local API" notice in the dashboard. Omit the field entirely when the menu does have API-accessible points. |

### Annotation precedence

A menu can have all three of `tip`, `note`, and `warning`. They are rendered in this order: `warning` first, then `note`, then `tip`. Use the right type for the content — do not use `warning` for general information just to make it stand out.

### `description` vs `note` vs `tip`

| Field | Purpose | Tone |
|---|---|---|
| `description` | What this menu is and what it does | Neutral, factual |
| `note` | Context, caveats, cross-references | Informational |
| `tip` | What to actually do, step by step | Actionable |
| `warning` | Safety risk, irreversible action, electricity cost | Urgent |

---

## Setting fields

Each entry in `settings` represents one firmware data point.

| Field | Required | Type | Description |
|---|---|---|---|
| `label` | ✅ | string | Human-readable name for this setting, as it appears in the controller's display menu. Used as the section divider label in the dashboard. |
| `point_id` | ✅ | integer or null | The firmware `variableId` for this data point. Must exist in the firmware's bulk fetch response. Invalid point IDs silently produce a "not enabled" placeholder in the dashboard. `null` is valid for a small number of controller-display-only settings that have no corresponding firmware register (currently 8) — these render as a label/range/annotations row with no entity attached. |
| `range` | ✅ | string | The valid value range, unit, and type — e.g. `5 – 30 °C`, `0 – 100 %`, `off/on`, `°C (read-only)`. Displayed in the section divider alongside the label. Free-form string — there is no machine parsing of this field. |
| `tip` | — | string | Green callout. Actionable guidance specific to this setting. |
| `note` | — | string | Blue callout. Context, caveats, cross-references specific to this setting. |
| `warning` | — | string | Red callout. Safety-critical information specific to this setting. |

### Annotation precedence for settings

Only one annotation is shown per setting — the highest-priority one present. Priority: `warning` > `note` > `tip`. If a setting has both a `note` and a `tip`, only the `note` is shown. Put the most important information in the highest-priority field.

---

## How the dashboard renders a setting

Each setting entry produces two rows in the dashboard entities card:

1. **Section divider** — `label  ·  range  ·  default: X` (default is looked up from live firmware metadata at build time; `default:` is omitted if unavailable)
2. **Entity row** — the HA entity for this point_id, resolved via the entity registry

If the entity is not enabled in the bridge, a `↳ not enabled` placeholder row appears instead of an entity row. If the entity is enabled but not yet in the HA registry (e.g. immediately after a mode change), it is also shown as not enabled — the dashboard will rebuild once the entity registers.

Dynamic points (points that only appear when a controlling switch is active) are never shown as direct setting rows. Instead, they are injected automatically below their controlling switch when active, with a `↳` section divider. Do not add dynamic points as `settings` entries.

---

## How `local_api: false` works

When a menu or submenu has `local_api: false`, it still appears as a section in the dashboard (so the menu structure mirrors the controller display completely) but no entity rows are shown. A notice is rendered instead:

> *This menu is visible on the controller display but its settings are not accessible via the local REST API.*

Use this for menus like alarm logs (3.4), version history (3.7), network settings (5.2), and controller-display-only actions (7.6 factory settings, 7.7 start guide).

---

## Adding a new menu

1. Find the correct position in the `menus` list — `id` values follow the controller's display menu numbering
2. Add the menu entry with at minimum `id`, `title`, `description`
3. Add `settings` entries for any data points accessible via the local REST API
4. Add `local_api: false` if the menu exists on the display but has no API-accessible points
5. Verify `point_id` values against the live firmware register list — use the Entity Manager card's search to find a point by name, or check the "Unplaced settings" tab in debug mode

The dashboard rebuilds automatically on the next add-on restart. Use the **Regenerate Dashboard** button in the management card to rebuild without restarting.

---

## Finding point IDs

Three ways to find the `point_id` for a setting:

1. **Entity Manager card** — search by name. The card shows the `point_id` (variableId) in the entity metadata panel.
2. **Unplaced settings tab** — visible in the Nibe Menus dashboard when the bridge runs with log level `debug`. Shows all firmware points not yet assigned to any menu, grouped by type.
3. **`all_data_points_raw.txt`** — included in the repository, contains the raw firmware register list from a reference installation. Search by register name.

**Never conflate a REST API point id with a Modbus register number.** Every data point carries both, and they are unrelated — point 26703 sits at Modbus register 5217, and point 6588 at Modbus register 0. `point_id` in this file always means the REST API `variableId`; the Modbus number lives in the point's own `metadata.modbusRegisterID` and reaches users as the `modbus_register` entity attribute. The hazard is that both are four- or five-digit integers, so mistaking one for the other yields a wrong mapping that still looks plausible.

This bites hardest when working from NIBE's documentation, because the sources disagree: the **firmware changelog identifies functionality by Modbus register** ("BT1 = 5217", "activated in menu 7.5.9.2"), while the manuals and the Entity Manager card work in point ids. To turn a changelog register into a point, look the number up against each point's `modbusRegisterID` — never search for a point whose id happens to equal it. And when quoting either kind of number in prose, say which it is; a bare number is what invites the confusion later.

**Search the firmware `description` field, not just the title.** NIBE's register titles are not consistent between related settings, so title-only matching misses a great deal. Where a register has a `description`, it usually enumerates the options verbatim (`0 = Small, 1 = Medium, 2 = Large`), which matches an installer manual's documented option list far more reliably than the title does.

(Where to obtain the manuals, and which editions these observations came from, is recorded in [reference-documents.md](reference-documents.md).)

**Beware menu numbering across manuals.** The accessory installer manuals (ACS 45, AXC 30, ERS 20/30/S10, F135, HRV) were written for several controller generations and use an older numbering scheme than the SMO S40's. `MENU 5.6 — FORCED CONTROL` in those documents is the SMO S40's `7.5.3`; `MENU 1.9.6 — FAN RETURN TIME` is its `1.2.5`; `MENU 5.2.4 — SYSTEM SETTINGS` is roughly its `7.2.1`. A menu number that appears in an accessory manual and not in `menu_structure.yaml` is usually this, not a genuine gap — match on the menu's *title and documented settings*, never on its number.

**Accessory registers live in the controller's map, whether or not the accessory is fitted.** NIBE's accessories (ECS, ERS, FLM, S135, AXC, ACS, PVT…) never run standalone — they always attach to an S-series controller — so the controller carries their registers permanently and the bulk fetch returns them regardless. This dump has 196 accessory-related points, 160 of them unmapped and 96 writable with live values, including 56 ECS points with nothing mapped at all. Do not assume a point is unreachable because the hardware is absent; check the dump. (A handful of settings genuinely are accessory-gated and only materialise when connected, the way the SG Ready registers do — menu 7.2.11's ERS settings are an example — but they are the exception.)

**Verify an "alternate register" before mapping it as one.** A few settings are exposed at two register addresses, and this file already carries some marked `alt. register`. Similar titles are not sufficient evidence. Point 4651 "Op. mode charge pump (EB101)" and 4729 "Operating mode circulation pump heating heat pump 1" share a range, a size and a live value, which looks conclusive until the cooling pair is checked: 4821 reads 0 while its apparent twin 4778 reads 1, so they cannot be the same setting. They refer to different pumps — the manual documents GP12 (the charge pump inside the heat pump) and GP10 (the external heating medium pump) as separate components. Compare live values across *every* member of a suspected alias group, not just one.

**Known menus not represented here.** The firmware changelog references menu `3.1.11` and two of its children — `3.1.11.8` (renamed from "EME20" to "Solar PV" in 2.13.5) and `3.1.11.13` ("Added missing sensor values in menu 3.1.11.13 for FLM", 3.0.10). The menu tree jumps straight from `3.1.10` to `3.1.12`, so that whole branch is absent. It has not been added because `3.1.x` is Operating info — read-only values — and this firmware exposes no read-only FLM registers at all; every FLM point is a writable setting, which belongs under accessory settings rather than operating info. Adding the menu would mean inventing its contents.

**Map what the firmware documents, not only what this installation exposes.** The bulk fetch returns what a *particular* controller has, and the reference dump comes from an installation with no pool, no ECS, no FLM and no ERS unit. A setting missing from that dump is not evidence it does not exist — only that this machine does not expose it. Since the add-on is used by people whose installations differ, a setting that NIBE documents conclusively belongs in this file even when the reference dump has no register for it: give it `point_id: null`, record the range and the source in a note, and it renders as a labelled row that a user with that hardware can recognise. The pool BT51 entries in 7.5.9.2, the ERS unit settings in 7.2.11 and the compressor start sequence in 7.1.6.6 are all documented this way.

**A documented setting with no register is normal.** Several menus the SMO S40 manual describes have no corresponding firmware register at all: `2.1` (More hot water), `2.2` (Hot water demand), `7.1.6.6` (Compressors, start sequence) and `7.2.11` (Vent. heat exchanger, ERS) were each checked against the full register list and none of their settings is exposed. Some are accessory-gated and only materialise when that hardware is connected, the way the SG Ready registers do. Leaving such a menu with no `point_id` is correct, not an oversight.

---

## Writing good annotations

**Tips** — be specific and actionable:

```yaml
tip: 'Always wait 24 hours after making a change before making another
  adjustment — the room temperature needs time to stabilise.'
```

```yaml
tip: 'Change by one step, wait 24 hours, reassess. Do not make multiple
  adjustments in the same day.'
```

**Notes** — add context that helps the user understand:

```yaml
note: 'Each offset step shifts the supply temperature by about 2.5°C
  at every outdoor temperature. The corresponding room temperature
  effect is roughly 1°C per step.'
```

**Warnings** — be direct about consequences:

```yaml
warning: 'This is an absolute override. Disabling permit heating prevents
  the system from heating under any circumstances, including frost
  protection. Only disable for troubleshooting with someone present.'
```

**Firmware-controlled values** — note that the firmware recalculates these:

```yaml
note: 'This value is recalculated continuously by the firmware based on
  outdoor temperature and heat demand. It cannot be set manually.'
```

---

## YAML formatting notes

- Always quote menu `id` values: `id: '1.1'` not `id: 1.1` — bare decimals are parsed as floats by YAML
- Use literal block scalars (`|`) or folded block scalars (`>`) for multi-line strings to avoid quoting issues
- Escape single quotes inside single-quoted strings by doubling them: `'don''t'`
- The file is loaded with `yaml.safe_load()` — no custom tags or anchors
