# NIBE reference documents

Where the manuals and firmware notes this project reasons from actually live,
and how to get the current edition of each.

Questions of the form *"is this point classified correctly?"*, *"which menu
does this setting belong to?"* and *"did this firmware release change
anything?"* are answered from NIBE's own documentation rather than by
inference. This file records the canonical locations so nobody has to
rediscover them, and so a stale local copy can be spotted.

The PDFs themselves are **not** in this repository. They live in a
developer-local collection (see CONTRIBUTING.md), alongside the equally
local `reference-dumps/`.

---

## The one genuinely stable URL

The firmware version history is published at a fixed, unversioned path and is
always current:

| Document | URL |
|---|---|
| NIBE S-series firmware changelog | <https://www.nibe.eu/webdav/files/myuplink_changelog/nibe-n.pdf> |
| NIBE S-series official Modbus register list | <https://installer.nibe.eu/download/18.47aa975e18a8b43315f342b/1696946128872/Modbus%20Register%20S-Series.pdf> |

This is the authoritative record of what each firmware release changed, back
to 4.3.5. It is worth checking first whenever a controller update lands.

**The Modbus register list is the single most valuable source found this
session.** It lists every input and holding register by its internal firmware
symbol name (e.g. `eMbHolding_eU8FanReturnTimeUnit4_0`), not the REST API's
often-generic display title, alongside its Modbus register number. Register
number matches directly to this project's own `modbusRegisterID` field (same
numbering space as `reference-dumps/`), so it can be used to look up the
internal name for *any* mapped or unmapped point. It resolved three separate
open mysteries in one pass by revealing structure the display titles hid
entirely — see "What the internal symbol names revealed" below. Saved locally
as `reference-pdfs/Modbus_Register_S-Series_official.pdf`.

---

## Manuals: the asset-store trap

NIBE serves manuals from an asset store with URLs of the form:

```
https://assetstore.nibe.se/hcms/v2.3/entity/document/<ID>/storage/<base64 of "<ID>/0/master">
```

**These IDs are pinned to a single edition, not to "the latest".** Part number
631929 has ID `877313` for edition IHB EN 2336-2 and `903683` for edition
IHB EN 2515-3 — a new edition is published under a *new* ID, and the old URL
keeps serving the old document indefinitely, with no indication that it has
been superseded.

So an asset-store URL recorded here is a **snapshot**, useful for confirming
which edition a conclusion was drawn from. It is not a way to stay current.

**To get the current edition, start from the product page**, which always
links the newest asset-store document. `dev/refresh-reference-docs.py`
automates exactly that — it reads the product pages, downloads whatever
installer manuals they link today, and reports what changed:

```bash
dev/refresh-reference-docs.py --dest /path/to/your/pdf/collection --dry-run
```

It keeps only installer manuals (`IHB`); product pages also carry user
manuals, sales brochures, energy labels and around thirty product
photographs. Nothing is deleted or overwritten — a document whose bytes match
a file already present is reported unchanged, so superseded editions stay for
diffing.

| Anchor | URL |
|---|---|
| NIBE document portal (all products) | <https://www.nibe.eu/en-eu/documents> |
| SMO S40 product page | <https://www.nibe.eu/en-eu/products/heat-pumps/air-water-heat-pumps/smo-s40> |

Extracting the links from a product page is enough to see what is current:

```bash
curl -sSL 'https://www.nibe.eu/en-eu/products/heat-pumps/air-water-heat-pumps/smo-s40' \
  | grep -oE 'https://assetstore\.nibe\.se/[^"]+' | sort -u
```

---

## Editions this project's conclusions were drawn from

Verified by downloading each and comparing byte counts against the local
copies, on 2026-09-11.

| Document | Edition | Asset-store ID | Bytes |
|---|---|---|---|
| Installer manual, SMO S40 | IHB EN 2515-3 (631927) | 903689 | 3 227 008 |
| Installer manual, SMO S40 UK | IHB EN 2515-3 (631929) | 903683 | 3 368 285 |
| Installer manual, S2125 | IHB EN 2525-1 (831880) | 905826 | 11 265 445 |
| Installer manual, S2125-14 | IHB EN 2525-1 (931059) | 905779 | 7 655 159 |
| Installer manual, VVM S320 | IHB EN 2327-1 (631794) | — | 4 103 075 |

The SMO S40 manuals held locally were already byte-identical to the published
editions. The S2125 and VVM S320 manuals were not: the first refresh replaced
an undated local S2125 copy with edition 2525-1.

### Announced products without documentation

S1157, S1257 and MHB S20 appear in the S-series firmware changelog — one
entry reads "First version with support for MHB S20 and S1157/S1257-8/13" —
but none has a product page in any nibe.eu category listing and none has an
installer manual published. They are announced, not yet documented. There is
nothing to fetch and nothing to map for them, so their absence from
`app/menu_structure.yaml` is not a gap to be closed; recheck when NIBE
publishes.

MHB S20 is additionally unclassified: nothing available says whether it is a
controller in its own right or an accessory attached to one, which is why it
is left out of the compatibility table in DOCS.md while the other two are
listed.

Not to be confused with **SHB 20**, whose manual *is* published. That is a
previous-generation product, not S-series: its installer settings live under
`MENU 5.x` with no `MENU 7.x` at all, it never uses the phrase "S-series",
and it mentions no REST API. Its menus must not be merged here — its
`1.9.2 External adjustment` is this structure's `1.30.3`, and `5.2.4
Accessories` roughly its `7.2.1`, so merging would duplicate semantics under
conflicting numbers.

### Find product pages by enumerating categories, not by guessing slugs

SVM S332's page is at `…/air-water-heat-pumps/split-svm-s332---ams-20`: a
split system is listed under both halves of its pair. No slug guess finds
that. List a category instead, and confirm the page carries documents:

```bash
curl -sSL 'https://www.nibe.eu/en-eu/products/heat-pumps/air-water-heat-pumps' \
  | grep -oE 'href="/en-eu/products/heat-pumps/air-water-heat-pumps/[^"]+"'
```

Doing that across the five relevant categories also turned up S2060 and
F2050, two air/water outdoor units that were missing from DOCS.md's
compatibility table.

### One document, two asset-store IDs

Asset-store IDs are not one-per-document either. Document `909505` and the ID
the S1156 product page links both serve edition IHB EN 2545-2 (part 831990) —
byte-for-byte identical, same SHA-256. `dev/refresh-reference-docs.py`
compares hashes against everything already present and reports "unchanged"
rather than saving a second copy under a new name.

### Accessories have no automatic route

`SOURCES` in the refresh script covers the products that have a page on
nibe.eu: SMO S40, S2125, VVM S320, and ERS S40/20/30. The remaining accessory
manuals in the local collection — ACS 45, AXC 30, F135, S135, ERS S10,
GV-HR 120 — cannot be refreshed automatically:

- nibe.eu has no accessories product category at all; the top-level product
  list offers only `smart-home-accessories` and equipment categories.
- A guessed slug does not fail loudly. `…/en-eu/products/ventilation/f135`
  returns HTTP 200 — it silently falls back to the Ventilation category page
  and yields no documents. Always confirm a candidate page really carries
  document links before adding it:
  `curl -sSL '<page>' | grep -c 'entity/document/'`
- `partner.nibe.eu`, which *does* list accessories, serves a TLS certificate
  that does not cover its own hostname, so it cannot be fetched without
  disabling certificate verification. That is not worth doing for a
  convenience script.

Individual accessory manuals can be found through search and do live in the
asset store, but only as edition-pinned URLs — which, being immutable, offer
no way to notice a newer edition. Those stay manual.

---

## Accessory manuals added this pass

Fetched and saved locally (`reference-pdfs/`, not on nibe.eu's automatic refresh
path — see "Accessories have no automatic route" above): ECS 40/41, EME 20,
FLM S45, RMU S40, HTS 40 installer manuals.

## Community Modbus exports (yozik04/nibe)

<https://github.com/yozik04/nibe/tree/master/nibe/data> holds per-model
Modbus register CSV/JSON exports contributed by real installations, generated
with ModbusManager. These use the same relative register numbering as this
project's own `modbusRegisterID` field (confirmed by exact register-number
match against `reference-dumps/`), unlike the older `smo40.csv`/`s1156.csv`
ad-hoc exports used earlier, which turned out to mix in an unrelated absolute
addressing scheme — see the `verify-cross-source-identity` lesson. Pulled this
session: `s330_s332.csv`, `s735.csv`, `s1155_s1255.csv`, `vvms320_vvms325.csv`.
Not yet pulled: `s2125.csv`, plus the F-series exports (out of scope for this
project).

These exports were useful for two things the manuals don't cover: confirming
a register is real and stable across multiple independent real installations
(not just this project's own single reference dump), and revealing structure
— e.g. that a firmware feature's registers repeat identically across several
models — that a single manual's prose does not spell out register-by-register.

## What the internal symbol names revealed

The official Modbus register list (see above) resolved every open thread
below in one pass, by exposing structure the REST API's display titles hid.
This is the kind of thing worth checking against for *any* future ambiguous
or generically-titled point, not just these:

**"Return time fan 4" was never "4 units" — it's up to 8 accessory-unit
slots, 4 speed levels each.** The internal names are
`eMbHolding_eU8FanReturnTimeUnit0_0..3` through `...Unit7_0..3` — `_0`
through `_3` are speed levels 1-4, and `Unit0` (modbus 115-118) is the
legacy single built-in unit found on S735/S1155 but not on hub-style
controllers (SMO S40, VVM S320/S325). This firmware's REST API exposes
`Unit4` through `Unit7` (modbus 2700-2715, IDs 21583-21598) — i.e. what had
been mapped as "unit 1-4" (21583-21586) was actually *one* unit's 4 speed
levels, mislabeled. Fixed: relabelled as "fan speed 1-4" for accessory unit
1, and added the remaining three accessory units (2-4) from the previously
unmapped twelve.

**The ERS 5-8 "second register" (previously removed as unconfirmed) is
`eMbHolding_eU8ErsProduct_4..7`** — the per-unit ERS model selector, the same
concept as the already-mapped "Product" field for the base unit. Restored,
labelled `Product (ERS 5-8)`.

**`Factor` (10691) is `eU8FactorWeatherControl`** — confirmed to be the
Weather Control influence factor (menu 4.4) after all. The RMU S40 and RG 10
theories investigated first were both wrong turns; this register was simply
missing its menu-4.4 home. Added there.

**Smart Energy Source's unnamed pricing block is fully named internally**:
`SesPriceElSpecial`/`SesPriceElNumeric` (electricity/compressor), `SesPriceFixedElTariff`/`...Num`
(a second, fixed-tariff-specific mode), `SesPriceShuntAddTariff`/`...Num`
(shunt-controlled additional heat), `SesPriceStepAddTariff`/`...Num`
(step-controlled additional heat). Each pair is a flat single-value tariff
mode, distinct from and additional to the already-mapped high/low tariff mode
for the same sources. Added to menus 4.7.1/4.7.3/4.7.4 alongside their
existing high/low tariff siblings.

One register did **not** get more confidence from its internal name:
`Changes have been made` (3904, modbus 2744) has the internal symbol
`eU8Solar` — unrelated to its REST title. Internal symbols can be stale
leftovers from a repurposed register slot, not a rename-on-repurpose
guarantee, so a symbol name is strong but not infallible evidence; this one
stays as documented (internal bookkeeping, left unmapped) on the strength of
its own REST title and the firmware changelog's "Smart Room Comfort
migration" explanation for its sibling (`External setting for adjustment
migrated`, 14710), not this Modbus list.

### A genuine pre-existing mismapping, found by cross-checking everything else

Beyond resolving the open threads above, this list was used to spot-check
every already-mapped point in `app/menu_structure.yaml` (~740 settings) for
cases where our label claims something more specific than the point's own
REST title, and the internal symbol name doesn't back that specificity up.
One real error turned up: point **12385**, labelled "Circulation active" in
menu 2.5 (Hot water circulation), had a REST title of plain **"Period"** and
an internal symbol of `eMbHolding_eU8EnergyLogSettingsPeriod` — an Energy Log
setting, unrelated to hot water circulation. It also directly contradicted
this menu's own warning, already present, that circulation has no direct
enable toggle and is only activated via AUX assignment in menu 7.4. Fixed:
removed 12385 from menu 2.5, and mapped the *actual* circulation status —
found by searching the official list's `Hwc`-prefixed symbols for one this
firmware's dump has a variableId for — to the correct point, **1829**
(`eMbInput_eU8HwcOnoff`, a read-only status, consistent with there being no
separate enable setting).

The broader lesson: a mismapping like this survives exactly because a
generic REST title (`Period`, `Factor`, `Status`, ...) gives no signal either
way, and our own label sounded plausible in isolation. The check that catches
it is comparing the REST title's *specificity* against the label's — a label
meaningfully more specific than its point's own bare title is the pattern
worth checking whenever this Modbus list is consulted for something else.

### All 58 `point_id: null` settings checked against this list — none resolved

Every settings entry with `point_id: null` in `app/menu_structure.yaml` (58
at the time of this check) was looked up individually: search the official
list for a plausible internal symbol matching the label, then check whether
that specific register (number + type together) has a variableId in this
project's own `reference-dumps/`. Result: **no new mappings** — every
candidate symbol found (`FanIncreaseAllowed`, `VbpAutoHw/Heat/Pool/Cool`,
`HwStartTemp_0-3`/`HwStopTemp_0-3`, `FlmSetPoint_0-3`,
`ERS40DesiredSupplyAirTemp`, `PhaseDetDone`, `ErsProduct` without a unit
suffix, and others) turned out to have no variableId in this firmware's
dump, confirming rather than contradicting the existing "no matching
register" notes. One entry (menu 2.4's "Stop temperature") was confirmed
correct as a deliberate cross-reference rather than a gap — its real
register (3702) is already mapped at menu 7.1.1.1.

This is a structurally different kind of check from the mismapping catch
above: that one started from a *known register number* (a mystery point) and
asked what it means, which this document answers exhaustively. This one
started from a *label with no register* and asked whether one exists at all
— and for a null point, that was already established in an earlier session
by checking the reference dump directly, so re-deriving it from the Modbus
list a second time was expected to (and did) reach the same answer. Worth
repeating only if the reference dump itself is refreshed from newer firmware,
not on a whim.

### Symbol-family mining: finding registers that exist but were never added

The null-pointer check above only catches settings that are already
*documented* in `menu_structure.yaml` but lack a register. It says nothing
about registers that genuinely exist in this firmware's own
`reference-dumps/all_points_en.json` but were never noticed at all — because
no manual or changelog entry ever pointed at them. The official Modbus list
makes those findable: group its symbols by stripping the trailing `_N` index
(e.g. `FanReturnTimeUnit4` from `..._0` through `..._3`), then check each
family member's `(modbusRegisterID, modbusRegisterType)` pair against the
reference dump. A family where several indices resolve to real variableIds
— especially ones with specific, self-explanatory REST titles — is a
genuine gap.

This pass found 17 candidate families. Ten were added:

- `AverageIndoor` — "Room average temperature, climate system 1-8 (BT50)",
  next to each system's own Room sensor factor settings (menus 1.3 / 7.2.4).
- `ECSBT3Return` / `BT2Supply` — return- and supply-line sensors for the
  extra-climate-system EP circuits (menu 7.2.4), alongside the accessory's
  existing zone-assignment settings.
- `LpControlTimer` — "Seconds of blank time left, charge pump 1-8" (menu
  3.1.10), following the same per-EB10x-cascade-unit condensing pattern
  already used for Block freq in menu 7.1.3.1.
- `DmStartTable` / `DmStopTable` — read-only live start/stop DM values for
  Smart Energy Source priorities 1-7 (menu 4.6), a direct companion to the
  already-mapped writable DM-start priority 1-5 settings.
- `FreezeProtectionStatusHeatExchanger` / `StateShunt` — per-cascade-unit and
  per-climate-system live status (menus 7.3.2.1 and 1.3/7.2.4 respectively).
- `SesAllowList` — 23 of 26 members mapped as "Permit (...)" read-only flags
  in menu 4.6 (per-cascade-unit EP14/EP15 permits, plus the generic
  prioritised/shunted/immersion-heater/OPT10 permits); the remaining 3 read
  "Permit (undefined)" and were excluded — see the ghost-register note below.
- `InputActivateExtcomp` — a second, genuinely distinct "External adjustment,
  climate system N" read-only family (input registers) next to the existing
  writable "External adjustment input (ECSN)" holding registers in menu
  7.2.4; confirmed distinct by register type, not just by name.
- `F135IndataU8RelaysStatus` — "Status, relay 0-5 (S135)" in menu 7.2.13.
- `FLMBT26CollectorIn` / `FLMBT27CollectorOut` — FLM S45 collector
  supply/return sensors (menu 1.2.3), using the existing FLM 1-4 numbering
  already in that menu (AZ10-13 in the firmware maps to FLM 1-4).

Three were deliberately left out:

- `FanActualSpeed` — REST title says "Fan mode N", contradicting the
  symbol's "ActualSpeed", with no manual corroborating either reading.
- `HeatsystemVbp` — REST title is the bare "Climate system N", carrying no
  more information than dozens of other already-mapped entities with that
  exact phrase.
- `Functionality` — resolves to raw, undecoded 32-bit values (e.g.
  `537658369`) with no documentation anywhere explaining the bit layout;
  exposing an unexplained number is not useful to a user.

### A corrected assumption about what makes a register "dead"

Early in this pass, `isWritable: false` combined with `minValue`/`maxValue`
both `0` was being treated as a signal to skip a candidate register. That
turned out to be wrong: `isWritable` is essentially determined by
`modbusRegisterType` alone — every `MODBUS_INPUT_REGISTER` in this dump is
`isWritable: false` by the Modbus protocol's own definition, not because the
specific register is unused, and `minValue`/`maxValue` of `0`/`0` is simply
how this firmware declares any read-only sensor-type register with no fixed
bounds (confirmed against point 4, "Current outdoor temperature", which is
unquestionably real and shows exactly this pattern). The signal that
actually works is REST title specificity — "Permit (undefined)" (found in
the `SesAllowList` family above) is the real red flag, not the numeric
range — together with whether the live value is a plausible reading or a
recognised "not connected" sentinel (`0`, or `-32768`/INT16 minimum, as seen
throughout the `BT2Supply` and `FLMBT26/27` families above, all legitimate
since this reference installation has no ECS or FLM hardware attached).

### A full orphan diff: points that exist but were never referenced at all

The symbol-family mining above only catches array-style groups (a symbol
with a trailing `_N` index). A structurally different and larger check is a
plain diff: every `point_id` present as a key in
`reference-dumps/all_points_en.json` (1169 total) against every `point_id`
actually referenced anywhere in `menu_structure.yaml` (789 at the time of
this check) — leaving **382 orphans** never looked at by any prior pass.

Of those, 357 have a matching symbol in the official Modbus document (the
pool the family-mining pass drew from; ungrouped singletons in there remain
for a future pass). The other **25 have no symbol in that document at all**
— their point_ids run from 24000 into the 55000s, well above anything the
official PDF covers, meaning this firmware is newer than that document's
edition. These were checked individually and mostly turned out valuable:

- An **energy log family** (produced/used energy for heating and cooling
  over the past hour, plus current power consumption and its per-component
  breakdown) — added to menu 3.3, which is also where a genuine bug was
  found and fixed: that menu carried `local_api: false` despite already
  having two real, working settings (`Total production`/`Total
  consumption`, registers 3821/3823) — the flag was silently hiding both
  from the dashboard entirely (`nibe_lovelace.py` returns immediately on
  `local_api: false`, before rendering any of the menu's own settings). The
  flag was almost certainly left over from before those two registers were
  found in an earlier session and never removed once they were added.
- **EEV control mode / EEV state (EB101)** — added next to the existing
  Superheat EEV trio in the EB101 diagnostics section.
- An **Outdoor liquid line sensor (BT39)** family — a base reading plus one
  per cascade unit (EB101-108) — added next to the existing evaporator/BT84
  sensor in the same EB101 diagnostics section.
- **Diverter valve hot water (QN10)**, a live status counterpart to the
  QN10 diverter valve already described (but only descriptively, with no
  status point) in menu 7.1.5.1's additional-heat settings.

Four were deliberately left out: `Ground water pump's control signal` is
writable with no documented range or confirmed function (the standing rule
against mapping unconfirmed writable settings applies directly); `Current
power` is too bare and risks reading as a confusing near-duplicate of the
energy log's own "Current power consumption"; `Class 1 alarm`, `Relay
status`, `Priority`, and `External setting for adjustment migrated` are all
bare, internally-flavoured titles with no manual or context to confirm what
they mean.

The remaining 357 symbol-matched orphans (singletons the family-mining pass
didn't group) are the natural next pass.

### The 357 symbol-matched orphans: a full pass, split into two pools

Grouping the 357 by symbol prefix immediately shows a sharp split: **215**
carry the `eSlaveHeatpumpArray_N_ModuleM_*` prefix — a deeply repetitive
per-cascade-slave, per-module diagnostic tree (compressor stats, pressures,
temperatures, alarms, each repeated across up to 8 slave units and 2
modules per unit). This pool is large enough and structurally different
enough (a 3-level fan-out: slave × module × field) to warrant its own future
pass rather than folding into this one. The other **142** were worked
through individually this pass.

Most of the 142 resolved cleanly onto existing menus once cross-checked by
symbol, following the same patterns established earlier in this document —
a read-only counterpart next to an existing writable setting, or a new
cascade-unit family following the EB101-108 condensing convention. Notable
groups added: ACS accessory sensors (Collector/Return line on EQ1, dump
signal GP20); hot water comfort sensors (BT70/82/83) and the AXC operating
modes alongside them; a large S135 cluster (five temperature sensors, six
status flags, two high-condenser alarms, actual pump/fan speed); the EME
20/Solar PV cluster (total energy, total average power, current power,
timers, PV operating mode); two Smart Price Adaption live offsets (pool,
cooling) alongside the existing writable "influence" settings; Smart
Energy Source's DM minimum value and hot-water priority selection; several
"operating mode" read-only companions for additional-heat and groundwater-
pump toggles that previously had only a writable side; a cascade-wide
compressor fleet overview (available/docked/used counts per function, plus
active-heat-pump count) and per-unit requested-compressor-frequency and
compressor-request flags; several cooling- and pool-blocking status flags;
and energy statistics (heating/cooling/hot-water compressor-only kWh, max
compressor frequency, overall frost protection status).

This pass also caught **two more stale `local_api: false` menus** of the
same kind found earlier for the Energy log menu — menu 2.1 ("More hot
water") and menu 2.3 ("External influence") both had `local_api: false`
despite this pass finding genuine, working registers that belong in them
(more-hot-water status/minutes-remaining/forced-start, and which subsystem
is currently controlling the hot water mode, respectively). Both flags were
removed and the settings added. Given two independent findings of the same
bug pattern, it's worth treating `local_api: false` as unverified rather
than authoritative whenever a mining pass turns up a real register for a
menu that carries it — the schema's own contract is clear (see "How
`local_api: false` works" below) but the flag can go stale exactly like any
other hand-maintained annotation once new registers appear in a firmware
update after the flag was set.

A number of candidates were deliberately left out, each for a specific,
checkable reason rather than a blanket "too obscure":

- **Unconfirmed writable functions**: `Ground water pump's control signal`
  and `Limit DM` are both writable with no manual, changelog, or symbol
  comment confirming what value they expect or what exactly they control —
  the standing rule against mapping unconfirmed writable settings applies
  directly, regardless of whether the declared range looks plausible.
- **Bare titles with no disambiguating symbol**: `Priority` (1758, distinct
  from the identically-bare `Priority` at 55000 found earlier), `Relay
  status` (24961, register 0 — the classic placeholder-register trap), `Is
  the compressor accessible`, `Oper. mode` (`eU8ExtSetOpmode`, whose symbol
  is no more specific than the title), `External guide status` / `Test
  guide value` (installer test-wizard internals), and the previously-known
  `Changes have been made` (`eU8Solar`) and `Functionality` bitmask family.
- **Redundant with an existing, more robust mechanism**: `Alarm number` and
  `Non module-specific alarm numbers` are generic top-level alarm counters
  that would sit alongside the bridge's own dedicated Active Alarms sensor
  and Reset Alarms button (see `nibe_ha_integration.py` /
  `nibe_mqtt_publisher.py`) without adding anything a user could act on
  differently; `Reset alarm` (a writable pulse/action register) risks the
  same duplication with the existing Reset Alarms button. `Current power`
  (29291, from the first no-symbol pass) was left out for the same
  reason once the EME-specific `eU32EmeTotalPower` "Current power" (6002)
  was confirmed to be a different, better-identified register.
- **Unconfirmed accessory identity**: `Fan speed (AZ30-GQ2/GQ3)` and the
  bare `AZ30-EB17` reference an "AZ30" designator that no manual in this
  project's collection documents — the readings themselves look plausible,
  but mapping them under a guessed accessory identity would be exactly the
  kind of unconfirmed-context speculation the project avoids.

The `eSlaveHeatpumpArray` cascade-diagnostics pool (215 points) remains as
the next natural pass, along with a second look at whether the symbol
comments in the official Modbus document (a handful of registers, like
`eU8EmeApi`, carry an inline `///<` comment) can help resolve any of the
titles left unmapped above.

### The eSlaveHeatpumpArray pool: EB101 mapped, EB100/EB102-108 deliberately left out

This family turned out to have real structure once grouped by its two
index positions (`eSlaveHeatpumpArray_<slave>_Module<module><field>` or
`eSlaveHeatpumpArray_<slave>_<field>`, slave 0-8, module 0-1): **slave index
1 is this installation's real, physically-connected EB101 outdoor unit**
(confirmed — many of its fields are already mapped elsewhere in this file
under the same variableIds, and the unmapped remainder reports genuinely
live, plausible values: compressor current, generated power, EEV
control-loop values, a second compressor module EP15 that reads as a
legitimately-absent second circuit rather than an error). **Slave index 0
("EB100") and slaves 2-8 (EB102-108) read as universally disconnected**
across every one of the roughly 30 distinct field types in this family, on
this single-unit reference dump.

The ~20-25 genuinely new EB101 fields were added next to the existing
EB101 diagnostics section. EB100 and EB102-108 were deliberately left
unmapped, after two proposed ways to handle their clutter risk turned out
not to fit cleanly:

- **HA `enabled_by_default: false`** (entity registers but starts disabled)
  would work uniformly regardless of each field's sentinel behaviour, but
  isn't wired into this codebase at all yet — it would need a new
  per-setting schema field threaded through to the single discovery-config
  call site in `nibe_mqtt_publisher.py`. A real code change, not just a
  content edit.
- **Lovelace conditional visibility** (hide a card when a value equals its
  "not connected" sentinel) only works cleanly for the family's
  BT-prefixed temperature fields, where `-32768` (INT16 minimum) is an
  unambiguous disconnection marker. Most of the family's other field types
  (compressor status, alarm counts, relay status, power, EEV opening,
  defrosting flags) report `0` for both "genuinely off" and "not
  connected" — indistinguishable from the value alone, so a
  value-based visibility gate would also hide a real cascade unit's
  legitimate idle/zero readings. It would also require restructuring this
  codebase's rendering: each menu section currently renders as one grouped
  `entities` card (confirmed in `nibe_lovelace.py`), and HA's `visibility:`
  is a card-level control, not a per-row one — so using it here would mean
  splitting every conditionally-hidden setting into its own small
  `type: conditional` card, fragmenting the dashboard.

Given neither approach cleanly covers the bulk of the family without a
real code change, and the sentinel-value ambiguity affects most of its
field types, EB100 and EB102-108 were left out as a deliberate scope
decision rather than mapped speculatively. Revisit this if the
`enabled_by_default` mechanism is ever built for other reasons — at that
point mapping the full cascade extension becomes low-risk.

## What the documentation does and does not cover

Two limits are worth knowing before spending time searching.

**NIBE ships undocumented registers.** Firmware 4.13.12 added eleven data
points. Not one of them is mentioned in the release notes, anywhere in the
changelog's history, in the SMO S40 installer manual, or in the S2125 manual —
including nine `BT39` sensors and a cluster cooling setting.

This was re-checked against the *newest published* S2125 installer manual
(IHB EN 2525-1) after refreshing, not just the older local copy: it documents
sensors BT3/12/14/15/16/17/28/84 and contains no occurrence of `BT39` or
"liquid line" at all. Absence from the manuals is therefore not evidence that
a register is unimportant, only that NIBE did not describe it.

**Accessory manuals use older menu numbering.** They are written for several
controller generations, so their menu numbers do not line up with the SMO
S40's. `MENU 5.6 — Forced control` in those documents is the SMO S40's
`7.5.3`; `1.9.6 — Fan return time` is its `1.2.5`; `5.2.4 — System settings`
is roughly its `7.2.1`. Comparing numbers directly against
`app/menu_structure.yaml` suggests fourteen missing menus, almost none of
which is actually missing. Match on a menu's title and its documented
settings, never on its number — see `docs/menu-structure-schema.md`.

## No water/water reference dump exists (a known, tracked gap)

Every reference point in this project -- `reference-dumps/all_points_en.json`,
the official Modbus document, everything mined this project's whole
history -- comes from one physical installation: an SMO S40, classified
`air_water` by `_detect_controller_family()`. There is no water/water
(ground-source) reference dump anywhere in this project's history.

19 settings across 4 menus (7.1.2.2, 7.1.2.7, 7.1.2.8, 4.11.6 -- all tagged
`family: water_water`, so they only ever render on a ground-source
installation's dashboard) carry `point_id: null` for exactly this reason:
GP1 (the internal heating medium pump), the brine circuit, and the ground
collector don't exist on an air/water unit at all, so there was never a
bulk fetch to check them against. This is a documentation gap, not a
confirmed absence -- a real water/water installation may expose working
registers for some or all of them, and menu_structure.yaml's own notes are
deliberately worded to avoid claiming otherwise.

Tracked in [GitHub issue #82](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/issues/82).
If a water/water reference dump is ever obtained, re-run the same
symbol-family/orphan-diff mining passes already applied to the air/water
dump (see the sections above) against it -- the methodology transfers
directly, only the input data is missing.
