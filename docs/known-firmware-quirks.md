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
`DIVISOR_OVERRIDES` in `nibe_entity_detection.py`.

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
- **Point 4562 (heating medium pump manual-speed switch) — firmware
  reports `unit: '%'`** on a plain 0=auto/1=manual switch. Overridden to
  no unit.
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
temperature sensor, that still declares `0`/`0`).

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

## Installer manual documents the wrong option range

- **Point 3281 ("Affect hot water")** — the installer manual documents
  this as a plain `off/on` toggle. The real firmware range is a 5-option
  select: `Small / Medium / Large / Medium / Mini`. `menu_structure.yaml`
  corrected to match the real firmware values, not the manual's claim —
  a reminder that "the manual says X" is evidence, not proof, when it
  conflicts with what the firmware itself reports.

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
