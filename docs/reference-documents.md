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
