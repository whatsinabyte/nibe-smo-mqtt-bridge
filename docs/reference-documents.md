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

This is the authoritative record of what each firmware release changed, back
to 4.3.5. It is worth checking first whenever a controller update lands.

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
links the newest asset-store document:

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
| User manual, SMO S40 | UHB EN 2208-1 (631965) | 849020 | 670 150 |

Both installer manuals were current as of that date — the product page linked
exactly these IDs.

The accessory manuals (ACS 45, AXC 30, ERS 20/30/S10/S40, F135, GV-HR 120,
HRV, S135, VVM S320) and the S2125 heat pump manuals have not had their
asset-store IDs recorded yet; they were read from the local collection. Their
product pages can be found through the document portal above.

---

## What the documentation does and does not cover

Two limits are worth knowing before spending time searching.

**NIBE ships undocumented registers.** Firmware 4.13.12 added eleven data
points. Not one of them is mentioned in the release notes, anywhere in the
changelog's history, in the SMO S40 installer manual, or in the S2125 manual —
including nine `BT39` sensors and a cluster cooling setting. The S2125 manual's
sensor list covers BT3/12/14/15/16/17/28/84 and no BT39 at all. Absence from
the manuals is therefore not evidence that a register is unimportant, only
that NIBE did not describe it.

**Accessory manuals use older menu numbering.** They are written for several
controller generations, so their menu numbers do not line up with the SMO
S40's. `MENU 5.6 — Forced control` in those documents is the SMO S40's
`7.5.3`; `1.9.6 — Fan return time` is its `1.2.5`; `5.2.4 — System settings`
is roughly its `7.2.1`. Comparing numbers directly against
`app/menu_structure.yaml` suggests fourteen missing menus, almost none of
which is actually missing. Match on a menu's title and its documented
settings, never on its number — see `docs/menu-structure-schema.md`.
