#!/usr/bin/env python3
"""Refresh the local NIBE reference documents to their current editions.

The manuals and firmware release notes this project reasons from are
developer-local data, not part of the repository (see
docs/reference-documents.md). This script fetches whatever NIBE currently
publishes and reports what has changed since the last run.

Why it is not simply a list of PDF URLs: NIBE serves manuals from an asset
store whose document IDs are pinned to a single *edition*, not to "the
latest". Part 631929 is ID 877313 for edition IHB EN 2336-2 and 903683 for
edition 2515-3; the superseded URL keeps serving the old document forever with
no indication it has been replaced. Recording those URLs as a way to stay
current does the exact opposite. The product page is the stable anchor -- it
always links the newest document -- so this script reads the product pages and
follows whatever they point at today.

Nothing is ever deleted or overwritten in place. A document whose bytes match
a file already present is reported as unchanged and discarded; anything new is
written alongside, so the previous edition remains available for diffing.

Usage:
    dev/refresh-reference-docs.py --dest /path/to/nibe-reference-pdfs
    dev/refresh-reference-docs.py --dest ... --dry-run
    NIBE_REFERENCE_PDFS=/path/... dev/refresh-reference-docs.py

Add a product by appending its page to SOURCES below; find the page through
NIBE's document portal at https://www.nibe.eu/en-eu/documents.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

# (label, kind, url) -- "page" is scraped for asset-store links, "file" is
# fetched directly. The changelog is the one NIBE URL that is unversioned and
# always current.
SOURCES: list[tuple[str, str, str]] = [
    (
        "S-series firmware changelog",
        "file",
        "https://www.nibe.eu/webdav/files/myuplink_changelog/nibe-n.pdf",
    ),
    (
        "SMO S40",
        "page",
        "https://www.nibe.eu/en-eu/products/heat-pumps/air-water-heat-pumps/smo-s40",
    ),
    (
        "S2125",
        "page",
        "https://www.nibe.eu/en-eu/products/heat-pumps/air-water-heat-pumps/s2125",
    ),
    (
        "VVM S320",
        "page",
        "https://www.nibe.eu/en-eu/products/heat-pumps/air-water-heat-pumps/VVM-S320",
    ),
    # Ground/water and exhaust-air models with a built-in controller. The bridge
    # supports these directly (see DOCS.md "Compatible Hardware"), and their
    # manuals document menus the SMO S40's does not -- the brine circuit above
    # all, which an air/water controller has no reason to describe.
    (
        "S1155",
        "page",
        "https://www.nibe.eu/en-eu/products/heat-pumps/ground-source-heat-pumps/s1155",
    ),
    (
        "S1156",
        "page",
        "https://www.nibe.eu/en-eu/products/heat-pumps/ground-source-heat-pumps/s1156",
    ),
    (
        "S1256",
        "page",
        "https://www.nibe.eu/en-eu/products/heat-pumps/ground-source-heat-pumps/s1256",
    ),
    ("S735", "page", "https://www.nibe.eu/en-eu/products/heat-pumps/exhaust-air-heat-pumps/s735"),
    ("S735C", "page", "https://www.nibe.eu/en-eu/products/heat-pumps/exhaust-air-heat-pumps/s735c"),
    ("ERS S40", "page", "https://www.nibe.eu/en-eu/products/ventilation/ers-s40"),
    ("ERS 20", "page", "https://www.nibe.eu/en-eu/products/ventilation/ers-20"),
    ("ERS 30", "page", "https://www.nibe.eu/en-eu/products/ventilation/ers-30"),
    # Not yet located as product pages on nibe.eu: ACS 45, AXC 30, F135, S135,
    # ERS S10 and GV-HR 120. Their manuals document accessory menus and the
    # registers those accessories add, so they are worth having — but a guessed
    # slug such as /en-eu/products/ventilation/f135 does not 404, it silently
    # falls back to the Ventilation category page and yields no documents at
    # all. Confirm any new page actually carries entity/document links before
    # adding it here:
    #   curl -sSL '<page>' | grep -c 'entity/document/'
]

# Only /entity/document/ — the same host also serves /entity/dam/ URLs, which
# are product photographs. A product page carries about thirty of them.
ASSET_RE = re.compile(r"https://assetstore\.nibe\.se/[^\"'\s<>]*?/entity/document/[^\"'\s<>]+")

# A manual identifies itself on page one as e.g. "IHB EN 2515-3 631927":
# document kind, language, edition, part number. Product pages also link sales
# brochures and datasheets, which carry no such marking.
#
# Only IHB — the installer manual — is wanted. That is the document that
# describes the menu structure, the component designations and the setting
# ranges this project checks its register handling against. The user manual
# (UHB) covers the same product for a homeowner audience and contains none of
# that detail.
MANUAL_RE = re.compile(r"\b(IHB)\s*([A-Z]{2})\s*(\d{4}-\d+)\s*(\d{6})\b")
UA = "nibe-smo-mqtt-bridge reference-doc refresh"
TIMEOUT = 90


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def pdf_title(data: bytes) -> str | None:
    """First-page title line, when pypdf is available.

    Deliberately optional: this is a dev utility and should not require a
    dependency the project does not otherwise declare. Without pypdf the
    documents are still fetched correctly, just named by their asset-store id.
    """
    try:
        import io
        import logging
        import warnings

        import pypdf
    except ImportError:
        return None
    try:
        logging.getLogger("pypdf").setLevel(logging.CRITICAL)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            text = pypdf.PdfReader(io.BytesIO(data)).pages[0].extract_text() or ""
    except Exception:  # noqa: BLE001 — a malformed PDF must not abort the run
        return None
    line = " ".join(text.split())[:120].strip()
    return line or None


def slug(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9.-]+", "_", text)).strip("_")[:110]


def existing_hashes(dest: Path) -> dict[str, str]:
    out = {}
    for f in sorted(dest.glob("*.pdf")):
        out[hashlib.sha256(f.read_bytes()).hexdigest()] = f.name
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--dest",
        default=os.environ.get("NIBE_REFERENCE_PDFS", ""),
        help="directory holding the local reference PDFs (or $NIBE_REFERENCE_PDFS)",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="report what would change, write nothing"
    )
    args = ap.parse_args()

    if not args.dest:
        print(
            "No destination given. Pass --dest or set NIBE_REFERENCE_PDFS.\n"
            "These PDFs are developer-local data and are never committed — see "
            "docs/reference-documents.md.",
            file=sys.stderr,
        )
        return 2
    dest = Path(args.dest)
    if not dest.is_dir():
        print(f"Not a directory: {dest}", file=sys.stderr)
        return 2

    print(f"==> Reference documents in {dest}")
    known = existing_hashes(dest)
    print(f"    {len(known)} PDF(s) already present\n")

    # (label, url, discovered) — "discovered" documents come off a product
    # page and must prove they are manuals; explicitly listed ones are wanted
    # by definition, which is how the firmware changelog survives the filter.
    targets: list[tuple[str, str, bool]] = []
    for label, kind, url in SOURCES:
        if kind == "file":
            targets.append((label, url, False))
            continue
        print(f"==> {label}: reading {url}")
        try:
            html = fetch(url).decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as e:
            print(f"    could not read the product page: {e}", file=sys.stderr)
            continue
        links = sorted(set(ASSET_RE.findall(html)))
        print(f"    {len(links)} document link(s) found")
        targets.extend((label, link, True) for link in links)

    new = unchanged = failed = brochures = 0
    print()
    for label, url, discovered in targets:
        try:
            data = fetch(url)
        except (urllib.error.URLError, OSError) as e:
            print(f"  FAILED  {label}: {e}")
            failed += 1
            continue
        if not data.startswith(b"%PDF"):
            print(f"  skipped {label}: not a PDF ({url})")
            failed += 1
            continue

        digest = hashlib.sha256(data).hexdigest()
        if digest in known:
            unchanged += 1
            continue

        title = pdf_title(data)
        marking = MANUAL_RE.search(title or "")
        if discovered and not marking:
            # Sales brochures and datasheets live on the same product pages.
            print(f"  ignored {label}: not an installer manual ({(title or url)[:56]})")
            brochures += 1
            continue

        if marking:
            kind, lang, edition, part = marking.groups()
            name = f"{slug(label)}_{kind}_{lang}_{edition}_{part}"
        else:
            name = slug(label)
        target = dest / f"{name}.pdf"
        n = 2
        while target.exists():
            target = dest / f"{name}__{n}.pdf"
            n += 1

        print(f"  NEW     {title or label}")
        print(f"          {len(data):,} bytes -> {target.name}")
        new += 1
        if args.dry_run:
            continue
        # Write via a temp file in the same directory, then rename, so an
        # interrupted download cannot leave a half-written PDF behind.
        fd, tmp = tempfile.mkstemp(dir=dest, suffix=".part")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, target)
        except BaseException:
            os.unlink(tmp)
            raise
        known[digest] = target.name

    print(
        f"\n==> {new} new, {unchanged} unchanged, "
        f"{brochures} not an installer manual, {failed} failed"
        + (" (dry run, nothing written)" if args.dry_run else "")
    )
    print("    Nothing was deleted; superseded editions stay for diffing.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
