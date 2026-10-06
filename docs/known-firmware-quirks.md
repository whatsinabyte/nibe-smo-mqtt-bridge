# Known NIBE firmware quirks

A catalog of confirmed defects, undocumented conventions, and misleading
metadata found in NIBE's own firmware and REST API — as opposed to bugs in
this bridge itself (those belong in `CHANGELOG.md` alone). NIBE has no
public bug tracker this project can file against, and no confirmation that
any of these are on their radar — this document is the only record of them
that exists anywhere, which is the reason to keep it rather than letting
each discovery live only in a changelog entry and fade from memory.

**Maintenance convention:** whenever a release fixes or discovers a
firmware-side defect (as opposed to a bug in this bridge's own code), add
or update an entry here in the same pass — mirror the finding out of the
changelog entry into its permanent, categorized home here, rather than
letting it exist only as changelog prose. Each entry should carry: what's
wrong, the evidence that confirmed it, how this bridge works around it, and
a link to the GitHub issue/PR/release where it was addressed, if any.

---

## Wrong scale or unit in a register's own metadata

The firmware's REST API reports a `unit` and `divisor` for every register,
and this bridge trusts that self-description by default — these are the
confirmed exceptions, corrected via `UNIT_OVERRIDES` /
`DIVISOR_OVERRIDES` / `RANGE_OVERRIDES` in `nibe_entity_detection.py`.

- **Point 29258 ("Production (PV Power)") — divisor wrong by 100x.**
  Declares `unit: kW`, `divisor: 1` (i.e. "the raw value is directly in
  kW"), but the real physical scale is 10 W per raw unit. Writing `17`
  (intended as 170 W) displayed as "17 kW" in Home Assistant while
  myUplink correctly read ~0.2 kW for the same PV inverter at the same
  moment. Confirmed on two independent controllers — the reporter's VVM
  S320 and this project's own SMO S40 reference dump both declare the
  identical broken `divisor: 1` — so this is firmware-wide, not specific
  to one model. Corrected divisor: 100. See
  [issue #84](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/issues/84),
  fixed in [v1.2.1](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/releases/tag/v1.2.1).
- **Points 25165/25166 ("Energy log — Current power consumption[,
  components]") — used to report the wrong unit entirely.** Firmware
  used to report an energy unit (`kWh`) for what are power points,
  requiring a hardcoded `unit: kW` + `device_class: power` override.
  Firmware later started reporting `kW` correctly on its own, making the
  override dead weight (removed in
  [v1.1.9](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/releases/tag/v1.1.9)).
  The one entry in this document confirming NIBE does sometimes fix these
  on their own, silently, in a later firmware version.
- **Point 3702 ("Stop temperature HW periodic increase", the
  anti-legionella setting) — minimum wrong by 10x.** Declares
  `minValue: 55` with `divisor: 10`, i.e. 5.5 °C, while the real minimum
  is 55 °C: the installer menu range is 55 – 70 °C, and the live value in
  all four reference dumps is raw `550` — that very minimum. The maximum
  (`700`, 70 °C) is right. Home Assistant's number entity therefore
  offered 5.5 – 55 °C as well. `RANGE_OVERRIDES` sets the raw minimum to
  `550`; it applies to both HA's range and the bridge's own range check
  on writes.
- **Point 4562 (heating medium pump manual-speed switch) — firmware
  reports `unit: '%'`** on a plain 0=auto/1=manual switch. Overridden to
  no unit.
- **Many registers report no unit at all.** The installer menus give one
  for each of these, and `UNIT_OVERRIDES` supplies it — always a unit
  Home Assistant recognises, so it also gets the matching device class:
  - 818–825, seconds of blank time left for charge pumps 1–8: `s`.
    Only pumps 1–4 used to be covered.
  - 1205–1219, the smart energy source start/stop DM values, and
    5294–5298, the DM start settings for them: `DM`. The settings used to
    be missing.
  - 995, injection pressure sensor (EB101-BP11): `bar`.
  - 849, EEV degree of opening (EB101): `%`.
  - 3282 and 3283, pool and cooling offset (SPA), and 4529 and 4685, the
    cooling/heating sensor set point and the hot water compressor step
    difference: `°C`.
  - 3861, floor drying ongoing time: `h`. 4030, more hot water minutes
    remaining: `min`.
  - 14314, available PV power reported over Modbus: `W`.
- **Points 50825/50827 (THS-10 accessory) — firmware reports no unit, or
  `%RH`,** for values that are plain percentages; HA's own unit
  auto-detection rejects `%RH`. Both overridden to `%`.

## Declared min/max that's a storage-width artifact, not a real limit

Several registers declare `minValue`/`maxValue` that turn out to be
nothing more than "however wide the integer type happens to be" — e.g. a
`u16` register capping at `65535` — rather than a value NIBE actually
chose to represent a real physical bound. Point 29258 above is the
clearest example: before the divisor fix, its declared max of `65535`
translated to "655.35 kW," which reads as a plausible-if-large number but
is really just 2¹⁶−1 with the (wrong) divisor applied — not a documented
NIBE statement about the largest PV array the SMO S40 supports. The same
caution applies to `minValue: 0, maxValue: 0` on read-only sensor-type
registers: this is the firmware's own convention for "no fixed bounds
declared," not evidence the register is dead or always zero — see
`nibe-ghost-register-detection` in project memory for the full reasoning
and a confirmed counter-example (point 4, a real, always-nonzero outdoor
temperature sensor, that still declares `0`/`0`). Point 12387 ("Months")
is another storage-width case: it declares `0`–`4095` (12 bits), while
the installer menu range is 1 – 24 months.

A declared maximum can also be off by one. Point 3745 ("Language")
declares `0`–`25`, i.e. 26 languages, and the installer menu even says
"26 language options". The controller's own drop-down offers 25, values
0–24, from English to Български, and nothing beyond. The firmware
changelog's most recent language addition, Bulgarian and Ukrainian in
2.26.3 (2024-03-08), is the end of that list, and no later release adds
one. The bridge's Language select offers the 25 real options, so the
phantom value 25 can't be written from Home Assistant.

This convention isn't specific to this bridge's own reference hardware —
an independent project reading the same local REST API,
[AndiHOK91/HA-Nibe-Local-REST-API](https://github.com/AndiHOK91/HA-Nibe-Local-REST-API),
hit the identical ambiguity and reached the same conclusion by a
different route: their `metadata_limits()` helper treats a declared
`minValue`/`maxValue` of `0`/`0` as untrustworthy rather than a real
degenerate range. Independent confirmation from a differently-architected
project (a native HA integration, not an MQTT bridge) reading the same
firmware is worth more than either project's own observation alone.

## A successful HTTP status doesn't mean the write was applied

A `PATCH /points` write can return HTTP 200 while the controller silently
refused it — the real verdict is a per-point result embedded in the JSON
response body (`"modified"` on success; `"error: read only value"` or
`"error: no such param"` on rejection), never the HTTP status code alone.
`nibe_api.py`'s `write_point()` has always checked this body, but it was
never written up here until now. Independently corroborated by two other
projects hitting the identical behavior: srcfl/ftw's `nibe_local` driver
carries the same check with the comment "the pump answers HTTP 200 even
when it refuses a write," and AndiHOK91/HA-Nibe-Local-REST-API's README
notes that writes to `time`-type points are deliberately blocked because
both the REST API and Modbus "don't reliably accept" them despite
appearing to succeed. Three independently-built projects landing on the
same root cause is strong confirmation this is a real firmware behavior,
not a misreading by any one of them. A likely, related cause worth
checking first for anyone hitting this: the Local REST API itself has a
read/write vs. read-only mode, set on the controller (installer menu
7.5.15) — in read-only mode, every write is silently accepted and
discarded the same way.

## `isWritable` doesn't reliably say whether a point can be written

Every point's metadata carries both a `modbusRegisterType` and an
`isWritable` flag. Across all four reference dumps (`en`, `de`, `nl`, `sv`,
identical in this respect) the flag follows the register type almost
everywhere: all 602 `MODBUS_INPUT_REGISTER` points are `isWritable: false`,
and 549 of the 566 `MODBUS_HOLDING_REGISTER` points are `isWritable: true`.
The other 17 holding registers are flagged `false`, and they don't read as
read-only values:

- 3478 "Reset alarm": a trigger-only register, so a reset that can't be
  written makes no sense.
- 4064 "Oper. mode", with its own value mapping (Auto / Manual / Add. heat).
- 4030 "More hot water (Number of minutes)".
- 55749 "Block new compressor" (range 0–1).
- 5222 "Delay timer EME".
- 3937 "Auxiliary operation on alarm".
- 1948 "Holiday function status".
- Eleven "External reading of value BT1 / BT25 / BT71 / BT5 / BT6 / BT7 /
  BT52 / BT50 / BT68" registers (26703–26711, 29971, 33194, 33196).

The one REST-only point, 32824 "Power limitation activation"
(`MODBUS_NO_REGISTER`), is flagged `true`.

**Workaround:** the bridge doesn't read `isWritable` at all
(`is_writable_point()` in `nibe_entity_detection.py`). A holding register,
or the REST-only point, is writable. An input register, or a point without
a register type, is not. The exception is `SAFETY_READ_ONLY_POINTS`, the
registers DOCS.md lists as intentionally unexposed. These are never written
and are shown as read-only sensors, whatever their register type: 55749
"Block new compressor", 55884 "Set point value power" and the 24 spot
prices. The last two are flagged writable by the firmware all the same.
The external sensor readings among the 17 above are writable: writing
them is a firmware feature (changelog 2.21.12, activated per sensor in
menu 7.5.9.2). The controller's own per-point answer to a write
(see the previous entry) has the final word, so a register the controller does
refuse is reported as rejected, not hidden.

Before this, the bridge refused writes to the 17 flagged registers itself,
without asking the controller. It showed 4064 as a select and 3478 as a
button whose every use ended in "Write Failed", and the other 15 as
read-only sensors.

Writability is judged per REST point, never per Modbus register number.
Input and holding registers have separate Modbus address spaces, and 74
register numbers appear in both. Modbus 22, for example, is input point 25
"Collector out (AZ10-BT27)" and holding point 3478 "Reset alarm".

## A restarting controller serves a partial point list

While the controller restarts, its REST API keeps answering but serves only
part of its points. During a 4.13.12 firmware update one controller served
1145 of its 1169 points for about a minute, across four polls, before the
rest returned. A point missing from one bulk response is therefore not
evidence that the firmware removed it.

**Workaround:** a missing point is published unavailable straight away, and
disabled only after five continuous minutes of absence (`_ABSENT_GRACE_S`,
see ARCHITECTURE.md). The bridge used to make one exception: in the 90
seconds after any write, while it watches for points the write shows or
hides, a missing point was taken for a dynamic point the write had hidden
and disabled at once, deleting its Home Assistant entity and history. Now
only a point already known to be dynamic is treated that way. Any other
point gets the same grace period as at any other time.

## Undocumented "multiple-of-ten" enum encoding

A recurring, entirely undocumented convention across several unrelated
registers: an enum-style status value encoded as `10`, `20`, `30`, `40`...
instead of the more obvious `0`, `1`, `2`, `3`. No installer manual or
changelog entry describes this — every mapping below was reverse-engineered
from real-world log evidence, one raw value at a time.

- **Point 3292 ("Operating mode, Smart Price Adaption")** — `10 = Off`,
  `30 = On`, confirmed across two independent installations (GitHub
  issue #35).
- **Point 1021 ("Operating mode PV panels")** — `10 = Off`, `40 = On`,
  same family, confirmed empirically (GitHub issue #29).
- **Point 1758 ("Priority")** — `10/20/30/40/60` mapped to
  Off/Hot water/Heating/Pool/Cooling.
- **Points 1762-1766 (shunt/pump operating-mode family)** — `10/20/30`
  (and `1766` additionally `40/50`) mapped to
  Off/Opening/Closing/Active/Passive variants.
- **Point 10614 ("Req. op. mode, SG Ready")** breaks the pattern — it
  uses a plain `0-3` range, confirmed on real hardware (firmware 4.12.8,
  S1256) to mean Cut off/Standard/Encouraged/Ordered. Two earlier guesses
  at this same point's encoding (a derived bit-encoding, and the removed
  3292 mapping applied here by mistake) were both wrong before real
  hardware testing settled it — see
  [issue #40](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/issues/40).
  Worth remembering: a derived guess is not a substitute for someone
  testing the real value, even when the guess looks internally consistent.
- **Point 3260 ("Operating mode SG Ready")** — the read-only counterpart
  to 10614, and *does* follow the multiple-of-ten family: `10` is
  confirmed (cross-checked against 10614's own confirmed default) to be
  the "no active grid signal" state equivalent to 10614's `1 = Standard`.
  The other three values (Encouraged/Ordered/Cut off) remain unconfirmed
  — still open in issue #40, needs a real installation with SG Ready
  actively triggered by a grid signal to observe them.

## Registers that exist but appear nowhere in NIBE's own documentation

Confirmed by checking not just the installer manual but the *entire*
multi-year firmware changelog and the newest published edition of every
relevant manual — absence from all of them is doing real work here, not
just "we didn't look hard enough."

- **Nine `BT39` sensors and a cluster cooling setting, added in firmware
  4.13.12.** Not mentioned in that version's own release notes, anywhere
  else in the changelog's history, in the SMO S40 installer manual, or in
  the newest published S2125 manual (IHB EN 2525-1) — which documents
  sensors BT3/12/14/15/16/17/28/84 and contains no occurrence of `BT39`
  or "liquid line" at all. See `docs/reference-documents.md` for the full
  cross-check.
- More broadly: the 522-point mapping pass in
  [v1.2.0](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/releases/tag/v1.2.0)
  found that installer manuals document only a fraction of what the
  firmware actually exposes — the official Modbus register document and
  the firmware changelog's own register mentions both turned up real,
  working registers no manual anywhere names.

## Inconsistent `description` field parsing across register families

Firmware `description` fields encode dropdown/enum options as free text,
but not with one consistent separator: most registers use `key=value`,
some use `key:value`, and at least one register mixes both separators
within the same string. Affected points showed a raw number field instead
of a dropdown, or lost some option labels, until parsing was widened to
handle every separator convention found across all 4 shipped translation
dumps.

Some option-based registers have no `description` at all, in any
language, so the firmware never says what their values mean. They show a
bare number until the options are hard-coded in `VALUE_MAPPINGS`, as a
live controller shows them: from its own menu (language English), or for a
status, observed alongside its other status entities:
- 7022, 7023 and 23141–23144, blocking actions (ERS 3–8): Level monitor /
  Blocked / Off.
- 5482, operating mode: Intermittent / Continuous / 10 days cont.
- 4692, charging method: Target temp / Delta temp.
- 4085, internal additional heat stepping mode: Linear / Binary.
- 56150, all sub units operating prio (read-only): 0 = Idle, 1 = Heating,
  2 = Cooling, observed on a live controller in step with its other status
  entities. The values for hot water and pool haven't been observed yet and
  show as plain numbers.

Without a translation for a label, the English label is shown, the same
fallback the API itself uses.

## Installer manual documents the wrong option range

- **Point 3281 ("Affect hot water")** — the installer manual documents
  this as a plain `off/on` toggle. The real firmware range is a 5-option
  select: `Small / Medium / Large / Medium / Mini`. `menu_structure.yaml`
  corrected to match the real firmware values, not the manual's claim —
  a reminder that "the manual says X" is evidence, not proof, when it
  conflicts with what the firmware itself reports.
- **Points 27294 and 10691 — manual and firmware disagree on the range,
  unresolved.** 27294 ("Auto mode start temperature for active cooling")
  is 15 – 40 °C in the installer menu and 0 – 40 °C in the firmware;
  10691 ("Factor") is 0 – 10 in the menu and 1 – 10 in the firmware.
  Which is right isn't confirmed; the bridge uses the firmware's range,
  and `menu_structure.yaml` keeps the manual's.

## Misclassified entity types (firmware gives no hint either way)

Not a metadata *error* exactly, but a repeated trap: several registers
report a multiple-of-ten operating-mode value (see above) with **no**
`description` field at all, and firmware's own metadata otherwise looks
identical to a genuine boolean — `isWritable`, `variableSize`, and
`range` give no distinguishing signal. Points 1021, 3292, 242/243/244/245,
998, and 24961 each individually triggered a fix and are now listed
explicitly in `nibe_entity_detection.py`'s `_BINARY_SENSOR_EXCLUSIONS`.
The related frost-protection-heat-exchanger family (632-638, 2804) hit
the same underlying trap but didn't need a separate exclusion-list entry
— their own 3-state `VALUE_MAPPINGS` entry (`{0: "Off", 1: "Active",
2: "Passive"}`) is enough on its own to keep them out of the
boolean-shaped auto-detection path. See also the dynamic reclassification
safety net (`EntityManager._reclassify_binary_sensor`) for hardware not
yet known about at write time.

A point with a `VALUE_MAPPINGS` entry needs no exclusion: only a mapping
of exactly the values 0 and 1 counts as binary. That covers 2701 ("Status
(ACS)", 3 = Passive / 7 = Active), which has the same boolean-looking
metadata and was never on the exclusion list.

A related size quirk: point 8060 ("Defrost Requested EB101") is a 0–1
on/off holding register like the many other flags, but declared `s8`
rather than `u8`, so the switch auto-detection (which requires `u8`)
made it a 0–1 number. Overridden to `switch` in `ENTITY_TYPE_OVERRIDES`.

## `firmwareId` isn't a firmware version

The device endpoint (`GET /api/v1/devices/{deviceId}`) returns a
`product` object whose `firmwareId` field the Local REST API document
lists only as `firmwareId: string`, with no description or example. On
this project's own SMO S40 it has always been `"nibe-n"`, across every
firmware update installed since the REST API came into use, so it doesn't
identify the installed firmware version. Version numbers are exposed as
four input-register points instead, and these are what sometimes change
with a firmware update: 802 "Version (S135)", 2453 "Version (EB101)",
2509 "Version (EB100)" and 14987 "Version, inverter (EB101)". What
`"nibe-n"` stands for isn't documented.

The version points are plain integers with no unit, and their encoding is
undocumented. Matched against myUplink on this project's own installation:

- **2509 (SMO S40):** `major << 8 | minor`. `1037` = 0x040D is 4.13;
  myUplink shows 4.13.12, so the patch level isn't in this register.
- **2453 (S2125-12):** `major << 12 | minor << 6 | patch`. `12481` is
  3.3.1, matching myUplink.
- **14987 (inverter):** a plain number. `61`, the same as myUplink.
- **802 (S135):** reads `0` on this installation, which has no S135.

The bridge decodes 2509 and 2453 into version strings and publishes 14987
and 802 as-is.

The bridge passes the value through unchanged: it's logged at startup
(`firmware: nibe-n`) and published as the HA device's `model_id`.
