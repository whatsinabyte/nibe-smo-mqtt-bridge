"""
nibe_lovelace.py
================
Lovelace UI provisioning for the Nibe S-Series MQTT Bridge.

Handles all interaction with the Home Assistant frontend:
  - Copying the Lovelace card JS file to /homeassistant/www/
  - Registering the card as a Lovelace resource via WebSocket
  - Creating and updating the Nibe Bridge and Nibe Menus dashboards
  - Debounced menu dashboard regeneration on entity enable/disable changes
  - Teardown on clean uninstall (NIBE_REMOVE_FRONTEND=1)

Public entry points called from generate_nibe_mqtt.main():
  copy_card_file()         — copy JS file on startup
  provision_lovelace_ui()  — open WebSocket, register resource, create dashboards
  schedule_menu_dashboard_regen() — wire debounced regen into entity_manager
  teardown_lovelace()      — remove dashboard/resource/card on clean uninstall
"""

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import yaml
from nibe_entity_detection import clean_string, clean_unit, get_value_mapping

if TYPE_CHECKING:
    from nibe_dynamic_map import DynamicPointMap
    from nibe_entity_manager import EntityManager
    from nibe_ha_integration import HAEntityRegistryWatcher

log_startup = logging.getLogger("nibe.startup")

# menu_structure.yaml is static for the lifetime of the process — it only
# changes on an add-on update, which requires a restart — so parsing it
# (~3,700 lines via PyYAML) once per path and reusing the result avoids
# repeating that parse on every dashboard regen. Regen fires on every entity
# enable/disable (not just dynamic point changes, see publish_enabled_state()
# in nibe_entity_manager.py), so on an active session this was real repeated
# work. Keyed by path rather than a single slot so callers that intentionally
# pass a different or nonexistent path (tests, mainly) don't see stale data.
_menu_structure_cache: dict[str, list] = {}
_menu_structure_cache_lock = threading.Lock()


def _reset_menu_structure_cache() -> None:
    """Clear the cached parsed menu_structure.yaml. Test-only hook."""
    with _menu_structure_cache_lock:
        _menu_structure_cache.clear()


def _load_menu_structure_yaml(yaml_path: str) -> list:
    """Load and parse menu_structure.yaml, caching the result per path.

    Raises on a missing/corrupt file exactly like a bare
    ``yaml.safe_load(open(...))`` would — callers keep their existing
    try/except handling. A failed load is never cached, so a transient
    error doesn't poison later calls with the same path.
    """
    with _menu_structure_cache_lock:
        if yaml_path not in _menu_structure_cache:
            with open(yaml_path, encoding="utf-8") as f:
                menu_data = yaml.safe_load(f)
            _menu_structure_cache[yaml_path] = menu_data.get("menus", [])
        return _menu_structure_cache[yaml_path]


def _copy_card_file() -> bool:
    """Copy the Lovelace card JS file to the HA www directory."""
    src = "/app/nibe-entity-manager-card.js"
    dst_dir = "/homeassistant/www"
    dst = os.path.join(dst_dir, "nibe-entity-manager-card.js")
    try:
        os.makedirs(dst_dir, exist_ok=True)
        shutil.copy2(src, dst)
        log_startup.info("Card file copied to %s", dst)
        return True
    except OSError as e:
        log_startup.warning("Could not copy card file to %s: %s", dst, e)
        return False


# ============================================================================
# LOVELACE SETUP  — resource registration + dashboard provisioning
# ============================================================================
#
# Design
# ------
# Everything that touches the HA Lovelace WebSocket API is consolidated here
# into a single function that opens one connection, runs all operations, and
# closes cleanly.  Two tasks are handled:
#
#   1. Resource registration — register (or update) the card JS file as a
#      Lovelace module resource so the frontend loads it.  Already existed;
#      folded in here to avoid opening the WebSocket twice.
#
#   2. Dashboard provisioning — create a dedicated "Nibe Bridge" dashboard
#      with the card pre-installed the first time the bridge starts.
#      Idempotent: checks for the dashboard's URL slug before creating.
#      Never touches the user's existing dashboards.
#
# Failure handling
# ----------------
# Every step is individually guarded.  Failures are logged as warnings, never
# as errors — Lovelace setup is best-effort.  If the WebSocket is unavailable
# (e.g. running outside HA) the whole function is a no-op.
#
# Dashboard config schema (HA storage mode)
# ------------------------------------------
# POST lovelace/dashboards/create → {id, url_path, ...}
# POST lovelace/config/save (with urlPath) → persists the view+card layout
#
# The dashboard is created in "storage" mode (not yaml) so that
# lovelace/config/save applies.  A yaml-mode dashboard ignores save calls.

_DASHBOARD_SLUG = "nibe-bridge"
_DASHBOARD_TITLE = "Nibe Bridge"
_DASHBOARD_ICON = "mdi:heat-pump"
_CARD_TYPE = "custom:nibe-entity-manager-card"


_MENU_DASHBOARD_SLUG = "nibe-menus"
_MENU_DASHBOARD_TITLE = "Nibe Menus"
_MENU_DASHBOARD_FLAG = "/data/lovelace_menus_provisioned"
_LOVELACE_FLAG = "/data/lovelace_provisioned"


def _format_point_value(point_id: int, point: dict, int_value: int) -> str:
    """Format a raw integer register value the same way for any point,
    whether it's a factory default or a current live value — so the two
    are always directly comparable.

    A select-type point with a known value mapping (get_value_mapping —
    the manual VALUE_MAPPINGS table, or a firmware-provided enum
    description) renders its actual label (e.g. "Compressor") instead of
    the raw integer. A boolean point with no such mapping (minValue 0,
    maxValue 1) renders as "off"/"on" rather than the raw "0"/"1" —
    matching the "off/on" range every such setting already uses elsewhere,
    instead of contradicting it. Anything else renders as a divisor-scaled
    number with its unit appended.
    """
    meta = point.get("metadata", {})
    min_val = meta.get("minValue", 0)
    max_val = meta.get("maxValue", 0)
    value_mapping = get_value_mapping(point_id, point)
    if value_mapping and int_value in value_mapping:
        return value_mapping[int_value]
    if min_val == 0 and max_val == 1:
        return "on" if int_value else "off"
    # `or 1` masks any falsy divisor default (None/0/dropped) — only a
    # truthy-but-wrong default (e.g. 2) is observable.
    divisor = meta.get("divisor", 1) or 1
    display = f"{int_value / divisor:g}"
    unit = clean_unit(meta.get("unit") or meta.get("shortUnit"))
    return f"{display} {unit}".strip() if unit else display


def _build_point_defaults(all_points_by_id: dict[int, dict]) -> dict[int, str]:
    """Build a point_id → formatted-default string map for menu annotations.

    Only includes points where the default is meaningful:
    - Writable MODBUS_HOLDING_REGISTER with a non-degenerate range
    - intDefaultValue != 0 or minValue != 0  (suppress ambiguous zeros,
      unless a value mapping makes the zero unambiguous — see below)

    The returned string is already formatted with divisor applied and unit
    appended, ready to embed in a section-divider label — see
    _format_point_value for the exact formatting rules.
    """
    defaults: dict[int, str] = {}
    for point_id, point in all_points_by_id.items():
        meta = point.get("metadata", {})
        if not meta.get("isWritable"):
            continue
        if meta.get("modbusRegisterType") != "MODBUS_HOLDING_REGISTER":
            continue
        min_val = meta.get("minValue", 0)
        max_val = meta.get("maxValue", 0)
        if min_val == max_val:
            continue
        int_default = meta.get("intDefaultValue")
        if int_default is None:
            continue
        value_mapping = get_value_mapping(point_id, point)
        if value_mapping and int_default in value_mapping:
            defaults[point_id] = value_mapping[int_default]
            continue
        if int_default == 0 and min_val == 0 and max_val > 1:
            continue
        defaults[point_id] = _format_point_value(point_id, point, int_default)
    return defaults


def _build_changed_from_default(
    bulk_data: dict[int, dict],
    point_defaults: dict[int, str],
) -> set[int]:
    """Return the point_ids whose current live value differs from their
    documented factory default.

    Only considers points that already have a computed default (from
    _build_point_defaults) — a point with no known/meaningful default is
    never flagged either way. Useful for spotting what an installer has
    already changed from stock on an unfamiliar installation.
    """
    changed: set[int] = set()
    for point_id, default_display in point_defaults.items():
        point = bulk_data.get(point_id)
        if not point:
            continue
        # entity_manager.bulk_data stores each point's live value as flat
        # "is_ok"/"raw_value" keys (see EntityManager._fetch_bulk_data's
        # in-place update) — not the nested {"value": {"isOk":...,
        # "integerValue":...}} shape used by the REST API's own point
        # objects (and by reference-dumps/all_points_*.json). Reading the
        # nested shape here previously matched nothing on any real point,
        # which is why the ✏️ changed-from-default badge never appeared —
        # confirmed on a real installation.
        if not point.get("is_ok"):
            continue
        current_display = _format_point_value(point_id, point, point.get("raw_value", 0))
        if current_display != default_display:
            changed.add(point_id)
    return changed


_MENU_REF_RE = re.compile(r"\bmenu\s+(\d+(?:\.\d+)*)", re.IGNORECASE)


def _resolve_view_id(ref_id: str, valid_top_level_menus: set[str]) -> str | None:
    """Resolve a menu reference id (e.g. "7.2.4") to whichever known view id
    is its longest matching dotted prefix.

    Most menus are a single unsplit view, so a reference like "7.2.4"
    resolves to "7" — the whole top-level menu 7 is one view. A menu
    tagged `split_submenus: true` (see _build_menu_dashboard_config) instead
    gives each of its immediate submenus its own view id (e.g. "7.2"), so
    the same "7.2.4" reference should resolve to the more specific "7.2"
    view rather than a "7" hub page. Checking prefixes longest-first makes
    both cases fall out of the same lookup without needing to know which
    menus are split. Returns None if no prefix at all is a known view id.
    """
    parts = ref_id.split(".")
    for i in range(len(parts), 0, -1):
        candidate = ".".join(parts[:i])
        if candidate in valid_top_level_menus:
            return candidate
    return None


def _linkify_menu_refs(
    text: str,
    valid_top_level_menus: set[str],
    current_top_level: str | None = None,
) -> str:
    """Turn "menu X.Y.Z" mentions in annotation text into clickable links
    to that menu's own dashboard view/tab.

    Every top-level menu (X) gets its own dashboard view at a stable path
    (menu-X), so a reference normally links to that top-level view, not a
    scroll position within it — Lovelace's markdown cards have no working
    in-page anchor mechanism for that (an `id` attribute on a heading either
    gets stripped by HA's markdown sanitizer or is unreachable across the
    card's shadow DOM boundary — confirmed by testing on a real
    installation: the link rendered but clicking it did nothing). A menu
    tagged `split_submenus: true` changes the granularity: its immediate
    submenus are each their own view (path menu-X-Y), so a reference is
    resolved to the most specific known view id — see _resolve_view_id. A
    reference whose id doesn't match any known view at all (e.g. a menu
    documented for a different accessory/language) is left as plain text
    rather than becoming a dead link.

    A reference that resolves to current_top_level — the view this text is
    itself being rendered into — is also left as plain text. Such a link
    would point at the exact page already open: since the URL doesn't
    change, clicking it triggers no navigation and just looks broken,
    whether the reference names the current section itself (e.g. a "menu
    1.2" mention inside menu 1.2's own text) or a sibling submenu on the
    same view (e.g. "menu 1.5" mentioned from within menu 1.2 — both
    resolve to the same menu-1 view when menu 1 isn't split).
    """
    if not text:
        return text

    def _replace(match: "re.Match[str]") -> str:
        full_ref = match.group(0)
        resolved = _resolve_view_id(match.group(1), valid_top_level_menus)
        if resolved is None:
            return full_ref
        if current_top_level is not None and resolved == current_top_level:
            return full_ref
        path = resolved.replace(".", "-")
        return f"[{full_ref}](/{_MENU_DASHBOARD_SLUG}/menu-{path})"

    return _MENU_REF_RE.sub(_replace, text)


def _build_overview_view(menu_structure: list) -> dict:
    """Build the landing view listing every top-level menu with a one-line
    summary and a tap-to-jump link to its own dashboard view/tab.

    Placed first so opening the dashboard cold shows an index instead of
    whichever menu happens to be first in menu_structure.yaml.
    """
    lines = [
        '<h2><font color="#9C1924">Nibe Menus — Overview</font></h2>',
        "",
        "Tap a menu below to jump straight to it.",
        "",
    ]
    for menu in menu_structure:
        menu_id = menu["id"]
        title = menu["title"]
        link = f"/{_MENU_DASHBOARD_SLUG}/menu-{str(menu_id).replace('.', '-')}"
        lines.append(f"### [Menu {menu_id} – {title}]({link})")
        # Only the first paragraph — a compact one-line summary, not the
        # full multi-paragraph menu description repeated here verbatim.
        summary = (menu.get("description") or "").strip().split("\n\n")[0].strip()
        if summary:
            lines.append(summary)
        lines.append("")
    return {
        "title": "Overview",
        "path": "overview",
        "cards": [{"type": "markdown", "content": "\n".join(lines)}],
    }


def _build_hub_children_card(children: list) -> dict:
    """Build the markdown card linking a split menu's hub view to each of
    its immediate submenus' own views — see _build_view_specs."""
    lines = ["### Jump to a section:", ""]
    for child in children:
        child_id = child["id"]
        title = child["title"]
        link = f"/{_MENU_DASHBOARD_SLUG}/menu-{str(child_id).replace('.', '-')}"
        lines.append(f"- [{child_id} {title}]({link})")
    return {"type": "markdown", "content": "\n".join(lines)}


def _build_jump_out_card(menu: dict) -> dict:
    """Build the markdown card standing in for a submenu that has been
    pulled out into its own separate view — used in place of recursing
    into it, when its parent isn't itself split (see the render_submenus
    loop in _build_menu_view). Styled like _build_overview_view's own
    per-item entries: title, link, one-line summary."""
    menu_id = menu["id"]
    title = menu["title"]
    link = f"/{_MENU_DASHBOARD_SLUG}/menu-{str(menu_id).replace('.', '-')}"
    lines = [f"### [{menu_id} {title} →]({link})"]
    summary = (menu.get("description") or "").strip().split("\n\n")[0].strip()
    if summary:
        lines.append(summary)
    return {"type": "markdown", "content": "\n".join(lines)}


def _find_split_descendants(menu: dict) -> list[dict]:
    """Find every `split_submenus: true` node within menu's subtree,
    without descending into one once found — that node's own descendants
    are its own concern once it's expanded (see _build_view_specs).

    This lets a single oversized submenu deep inside an otherwise
    reasonably-sized menu (e.g. menu 3 "Info", where nearly all of its
    bulk sits in menu 3.1) be pulled out into its own view without forcing
    every one of menu 3's other, already-small children into a split too.
    """
    found = []
    for sub in menu.get("submenus", []) or []:
        if sub.get("split_submenus"):
            found.append(sub)
        else:
            found.extend(_find_split_descendants(sub))
    return found


def _build_view_specs(menu_structure: list) -> list[dict]:
    """Flatten menu_structure into one entry per dashboard view to build.

    Most menus produce exactly one entry (the menu itself, rendered fully
    with all its submenus recursively flattened into one view — the
    existing behaviour). A menu tagged `split_submenus: true` instead
    produces one "hub" entry for the menu itself — its own header plus a
    list of links to its children, no recursion into them here (see
    render_submenus in _build_menu_view) — and one further entry per
    immediate submenu, each rendered fully as its own separate view. This
    keeps a large menu (e.g. installer settings — hundreds of settings
    across a dozen submenus) from being flattened into a single, very long
    scrolling tab, and lets cross-references between its submenus become
    real, working links instead of same-page dead text (see
    _linkify_menu_refs).

    The flag isn't limited to top-level menus: a submenu buried inside an
    otherwise-unsplit menu can carry it too (see _find_split_descendants),
    getting pulled out into its own view while the rest of its ancestor's
    tree still flattens normally — the spot where it would have appeared
    inline instead gets a short "jump to its own view" card (see
    _build_jump_out_card in _build_menu_view's own rendering).

    A "children" value of None marks a normal, fully-recursive entry; a
    list (even empty) marks a hub entry. "subview" marks an entry that
    should render as a Lovelace subview (see _build_menu_dashboard_config)
    — true for anything reachable only via a hub's links or a jump-out
    card, which has no need to also clutter the dashboard's top tab strip.
    """
    specs: list[dict] = []

    def _expand(menu: dict, subview: bool) -> None:
        if menu.get("split_submenus"):
            children = menu.get("submenus", []) or []
            specs.append({"menu": menu, "children": children, "subview": subview})
            for child in children:
                _expand(child, subview=True)
        else:
            specs.append({"menu": menu, "children": None, "subview": subview})
            for descendant in _find_split_descendants(menu):
                _expand(descendant, subview=True)

    for menu in menu_structure:
        _expand(menu, subview=False)
    return specs


_MASONRY_COLUMN_CHUNKS = 3


def _group_cards_for_masonry(cards: list) -> list:
    """Regroup a flat markdown/entities card sequence (as returned by
    _build_menu_view) into a handful of large sequential vertical-stacks,
    one per intended column.

    _build_menu_view emits one markdown card per section, immediately
    followed by that section's entities card when it has one. Wrapping the
    whole view's cards in a single outer vertical-stack (the original
    behaviour) forces everything into one column regardless of screen
    width. But HA's masonry view lays out cards by greedily dropping each
    one into whichever column is currently shortest — if every section
    became its own small card (an earlier version of this function did
    exactly that), sections get interleaved across columns by height
    rather than by menu number, so scrolling down one column and then the
    next reads the menu numbers out of order.

    Instead, sections are first grouped individually (a markdown card plus
    its entities card, paired), then those groups are packed in original
    order into _MASONRY_COLUMN_CHUNKS contiguous, roughly-equal-weight
    chunks — each chunk becomes one big vertical-stack. Masonry still
    balances chunks across columns by height, but because there are only a
    few large chunks instead of many small ones, each column ends up
    holding one contiguous run of sections in their original numeric
    order — like reading down the left page of a book, then the right,
    rather than jumping line by line between them.

    A chunk (or a section within a chunk that has no entities card — e.g.
    the trailing footer) that ends up holding only one card is returned
    bare rather than wrapped in a pointless single-card vertical-stack.
    """
    sections: list[list[dict]] = []
    for card in cards:
        if card.get("type") == "markdown" or not sections:
            sections.append([card])
        else:
            sections[-1].append(card)

    if len(sections) <= 1:
        return [
            section[0] if len(section) == 1 else {"type": "vertical-stack", "cards": section}
            for section in sections
        ]

    def _weight(section: list[dict]) -> int:
        # Approximates rendered height: the markdown card itself, plus one
        # unit per entity row in its entities card (if any) — so a section
        # with 40 settings counts for much more than one with 2.
        w = 1
        for card in section:
            if card.get("type") == "entities":
                w += len(card.get("entities", []))
        return w

    weights = [_weight(section) for section in sections]
    chunk_count = min(_MASONRY_COLUMN_CHUNKS, len(sections))
    target = sum(weights) / chunk_count

    chunks: list[list[list[dict]]] = [[]]
    running = 0
    for section, weight in zip(sections, weights, strict=True):
        chunks[-1].append(section)
        running += weight
        if len(chunks) < chunk_count and running >= target * len(chunks):
            chunks.append([])

    result = []
    for chunk in chunks:
        if not chunk:
            continue
        flat_cards = [card for section in chunk for card in section]
        result.append(
            flat_cards[0]
            if len(flat_cards) == 1
            else {"type": "vertical-stack", "cards": flat_cards}
        )
    return result


def _build_dynamic_injection(
    dynamic_point_map: "DynamicPointMap",
    active_dynamic_points: set[int],
    registry_watcher: "HAEntityRegistryWatcher",
    all_points_by_id: dict,
    point_defaults: dict[int, str] | None = None,
) -> dict[int, list[tuple[str, str, str, str]]]:
    """Build controlling_point_id → [(entity_id, title, range_str, default_str), ...] map."""
    injection: dict[int, list[tuple[str, str, str, str]]] = {}
    for entry in dynamic_point_map.values():
        if entry.firmware_removed:
            continue
        active_for_entry = entry.all_known_dynamic_points() & active_dynamic_points
        if not active_for_entry:
            continue
        items = []
        for dyn_pid in sorted(active_for_entry):
            eid = registry_watcher.entity_id_for(dyn_pid)
            if eid:
                point = all_points_by_id.get(dyn_pid, {})
                title = point.get("display_title") or point.get("title") or f"Point {dyn_pid}"
                meta = point.get("metadata", {})
                # Any falsy .get() default here (None, dropped) is masked
                # by the trailing `or 1` — only a truthy non-1 default
                # (e.g. 2) would be observable. Verified empirically.
                div = meta.get("divisor", 1) or 1
                # `or 0` (not just a .get default) also covers the API
                # sending an explicit "minValue"/"maxValue": null — .get()'s
                # default only applies when the key is absent, so a
                # present-but-null value would otherwise reach the division
                # below as None and raise TypeError. Same falsy-default
                # masking as divisor above applies to the .get() defaults
                # themselves (0 vs None/dropped) — verified empirically.
                mn = (meta.get("minValue", 0) or 0) / div
                mx = (meta.get("maxValue", 0) or 0) / div
                unit = clean_unit(meta.get("unit"))
                rng = f"{mn:g} – {mx:g}{' ' + unit if unit else ''}"
                dflt = (point_defaults or {}).get(dyn_pid, "")
                items.append((eid, title, rng, dflt))
        if items:
            injection[entry.point_id] = items
    return injection


def _build_menu_view(
    menu: dict,
    registry_watcher: "HAEntityRegistryWatcher",
    known_dynamic: set[int] | None = None,
    point_defaults: dict[int, str] | None = None,
    dynamic_injection: dict[int, list[tuple[str, str, str, str]]] | None = None,
    valid_top_level_menus: set[str] | None = None,
    changed_from_default: set[int] | None = None,
    render_submenus: bool = True,
) -> list:
    """Build a list of Lovelace cards for a single top-level menu.

    Parameters
    ----------
    menu :           Top-level menu dict from menu_structure.yaml.
    registry_watcher: HAEntityRegistryWatcher — resolves point_id → entity_id.
    known_dynamic :  Points seen at least once in a bulk fetch on this
                     installation.  A point absent from this set has never
                     appeared — hardware not installed or feature inactive.
                     Dynamic points are skipped from this view's rows
                     entirely regardless of active/absent state (below) —
                     when active they appear via dynamic_injection instead.
    point_defaults : point_id → formatted default string, from
                     _build_point_defaults().  Appended to section-divider
                     labels as "· default: X" where present.
    dynamic_injection : controlling_point_id → list of entity_ids for active
                     dynamic points controlled by that point.  These are
                     injected below the controlling entity row in the card,
                     labelled with a ↳ indent to show the relationship.
    valid_top_level_menus : top-level menu ids actually present in this
                     installation's menu_structure.yaml. Used to turn
                     "menu X.Y.Z" mentions in annotation text into clickable
                     links to that menu's view — see _linkify_menu_refs.
    changed_from_default : point_ids whose current live value differs from
                     their documented factory default, from
                     _build_changed_from_default(). Marked with a ✏️ badge
                     on the section-divider label.
    render_submenus : When False, render only `menu`'s own header (and its
                     own direct settings, if any) — skip recursing into its
                     submenus. Used to build the hub view for a menu tagged
                     `split_submenus: true`, whose immediate submenus are
                     instead each rendered as their own separate call to
                     this function — see _build_menu_dashboard_config.
    """
    known_dynamic = known_dynamic or set()
    point_defaults = point_defaults or {}
    dynamic_injection = dynamic_injection or {}
    valid_top_level_menus = valid_top_level_menus or set()
    changed_from_default = changed_from_default or set()
    # The view this call is rendering into — matches the "menu-<id>" path
    # built for it in _build_menu_dashboard_config, dots included, so that a
    # split-out submenu view (e.g. id "7.2") is recognised as its own view
    # distinct from a same-numbered reference into a sibling split view
    # (e.g. "7.3") — see _resolve_view_id.
    current_top_level = str(menu["id"])
    cards = []

    def _linkify(text: str) -> str:
        return _linkify_menu_refs(text, valid_top_level_menus, current_top_level)

    def _alert(alert_type: str, title: str, text: str) -> str:
        """Render an HA native alert box (HA 2022.9+).

        alert_type: "warning", "info", "error", "success"
        Renders as a coloured box with left accent and icon in HA.
        Falls back gracefully to plain text in older HA versions.
        """
        return f'<ha-alert alert-type="{alert_type}" title="{title}">{text.strip()}</ha-alert>'

    # depth's own default (2) is unreachable — every call site below always
    # passes depth explicitly (either `depth=2` or `depth + 1`), and the
    # top-level call's own `depth=2` matches this default anyway, so
    # dropping that kwarg is also unobservable. Verified empirically.
    def _render_section(m: dict, depth: int = 2) -> None:
        # ── Description + callouts markdown card ────────────────────────────
        # Section heading embedded in the markdown using colour-coded HTML.
        # Top-level menus use larger heading; submenus use smaller heading.
        # Use HTML heading tags so colour and heading size both render correctly.
        # Markdown ## inside <font> is not parsed as a heading by HA's renderer.
        htag = "h2" if depth <= 2 else "h3" if depth == 3 else "h4"
        md_lines: list[str] = [
            f'<{htag}><font color="#9C1924">Menu {m["id"]} – {m["title"]}</font></{htag}>',
            "",
        ]

        if m.get("description"):
            md_lines.append(_linkify(m["description"].strip()))
            md_lines.append("")

        # Every blank-line `md_lines.append("")` below (menu-level and
        # per-setting callouts alike) is purely cosmetic markdown spacing —
        # all existing tests use assertIn/splitlines() substring checks on
        # individual lines, never exact-equality on the fully joined
        # content, so a wrong separator string is unobservable. Verified
        # empirically.
        # Menu-level callouts using ha-alert for native coloured boxes
        if m.get("warning"):
            md_lines.append(_alert("warning", "Warning", _linkify(m["warning"])))
            md_lines.append("")
        if m.get("note"):
            md_lines.append(_alert("info", "Note", _linkify(m["note"])))
            md_lines.append("")
        if m.get("tip"):
            md_lines.append(_alert("success", "Tip", _linkify(m["tip"])))
            md_lines.append("")

        # Items not available via local API
        if m.get("local_api") is False:
            md_lines.append(
                _alert(
                    "info",
                    "Not available via local API",
                    "This feature is configured on the controller display and has no local API register.",
                )
            )
            md_lines.append("")
            if md_lines:
                cards.append({"type": "markdown", "content": "\n".join(md_lines)})
            return

        # Collect per-setting callouts BEFORE appending the markdown card
        # so they are included in the same card as the section description.
        for s in m.get("settings", []):
            label = s.get("label", "")
            # s_warning/s_note/s_tip's None/dropped defaults are masked —
            # only ever checked via truthiness (`if`/`elif`), where None
            # and '' are equally falsy. Verified empirically.
            s_warning = s.get("warning", "")
            s_note = s.get("note", "")
            s_tip = s.get("tip", "")
            if s_warning:
                md_lines.append(_alert("warning", label, _linkify(s_warning)))
                md_lines.append("")
            elif s_note:
                md_lines.append(_alert("info", label, _linkify(s_note)))
                md_lines.append("")
            elif s_tip:
                md_lines.append(_alert("success", label, _linkify(s_tip)))
                md_lines.append("")

        # Render section heading + description + callouts as a markdown card
        cards.append({"type": "markdown", "content": "\n".join(md_lines)})

        # Build a single entities card for this section using HA's native
        # section-divider rows (HA 2024.1+) to label each control.
        entities_rows = []

        for s in m.get("settings", []):
            point_id = s.get("point_id")
            label = s.get("label", "")
            # None/dropped default is masked — only ever checked via
            # `if rng:` below. Verified empirically.
            rng = s.get("range", "")

            # Dynamic points: skip entirely regardless of active state.
            # When active they appear via injection below the controlling point.
            # When inactive they should not appear at all.
            # is not None, not a truthy check — see _collect_menu_points
            # for why point_id: 0 must not be treated the same as no point.
            if point_id is not None and point_id in known_dynamic:
                continue

            # Resolved ahead of the section label below so the ✏️ badge can
            # be gated on it — see the comment there for why.
            entity_id = registry_watcher.entity_id_for(point_id) if point_id is not None else None

            # Section divider: [🟠 ✏️] label · range · default (where known).
            # The badge flags a value already changed from its documented
            # factory default — useful when auditing an unfamiliar
            # installation to see what's been touched. Only shown when the
            # point also has a resolved, enabled entity: a not-yet-enabled
            # entity has no dashboard control to act on, and its bulk-fetch
            # value is unreliable (the firmware may not be actively
            # reporting it while disabled) — flagging it as "changed"
            # produced a wall of false positives on every not-enabled row.
            # HA's native entities card gives section-divider labels no
            # custom text color without an extra custom-card dependency
            # (e.g. card_mod), so the leading colored-dot emoji is what
            # makes this pop against the surrounding text rather than
            # relying on the pencil alone.
            changed_badge = "🟠 ✏️ " if entity_id and point_id in changed_from_default else ""
            section_label = f"{changed_badge}{label}"
            if rng:
                section_label += f"  ·  {rng}"
            if point_id is not None and point_id in point_defaults:
                section_label += f"  ·  default: {point_defaults[point_id]}"
            entities_rows.append(
                {
                    "type": "section",
                    "label": section_label,
                }
            )

            if point_id is not None:
                if entity_id:
                    entities_rows.append({"entity": entity_id})
                    for dyn_entity_id, dyn_title, dyn_rng, dyn_dflt in dynamic_injection.get(
                        point_id, []
                    ):
                        divider = f"↳ {dyn_title}  ·  {dyn_rng}"
                        if dyn_dflt:
                            divider += f"  ·  default: {dyn_dflt}"
                        entities_rows.append({"type": "section", "label": divider})
                        entities_rows.append({"entity": dyn_entity_id})
                elif point_id not in known_dynamic:
                    entities_rows.append(
                        {
                            "type": "section",
                            "label": "↳ not enabled",
                        }
                    )

        if entities_rows:
            cards.append(
                {
                    "type": "entities",
                    "entities": entities_rows,  # type: ignore[dict-item]
                }
            )

        # Recurse into submenus — except one flagged split_submenus itself,
        # which has been pulled out into its own separate view (see
        # _build_view_specs/_find_split_descendants) even though this
        # menu's own tree isn't otherwise split. A short "jump to its own
        # view" card stands in for it here instead.
        if render_submenus:
            for sub in m.get("submenus", []):
                if sub.get("split_submenus"):
                    cards.append(_build_jump_out_card(sub))
                else:
                    _render_section(sub, depth + 1)

    _render_section(menu, depth=2)

    # Footer
    cards.append(
        {
            "type": "markdown",
            "content": "---\n*Source: NIBE SMO S40 installer manual — Chapter 9, Control – Menus*",
        }
    )

    return cards


def _build_unplaced_view(
    bulk_data: dict[int, dict],
    menu_yaml_points: set[int],
    registry_watcher: "HAEntityRegistryWatcher",
    point_defaults: dict[int, str],
) -> dict | None:
    """Build a debug-only 'Unplaced settings' view.

    Shows all HOLDING/INPUT register points from the live bulk fetch that are
    not referenced anywhere in menu_structure.yaml. Uses bulk_data (not
    all_points_by_id) so ALL firmware points are shown, including those never
    enabled by the bridge.

    Grouped into:
    - Writable singles: unique writable HOLDING points worth reviewing
    - Writable groups: repetitive CS2-8 / zone / ECS / FLM series
    - Read-only: INPUT register sensors not in YAML

    Only included when debug_mode=True.
    """
    # Patterns that indicate a multi-system series — grouped separately.
    # Matched with re.IGNORECASE below, so a case-flip mutation of any
    # pattern here is unobservable — only a change to the pattern text
    # itself (e.g. an XX-wrap) would alter what actually matches.
    _GROUP_PATTERNS = [
        r"climate system [2-8]",
        r"zone \d+",
        r"ECS\d+",
        r"FLM [2-4]",
        r"EB1[0-9][2-9]",
        r"ERS [5-8]",
        r"RMU",
        r"smart energy source",
        r"tariff",
        r"return time fan",
        r"filter replacement",
        r"desired room temperature for zone",
    ]

    unplaced_writable = []
    unplaced_grouped = []
    unplaced_readonly = []

    for point_id, point_data in sorted(bulk_data.items()):
        if point_id in menu_yaml_points:
            continue
        meta = point_data.get("metadata", {})
        # A None/dropped default is unobservable — reg is only ever
        # checked via `not in (...)`, which a wrong-but-still-absent value
        # also fails. Verified empirically.
        reg = meta.get("modbusRegisterType", "")
        if reg not in ("MODBUS_HOLDING_REGISTER", "MODBUS_INPUT_REGISTER"):
            continue
        # `or 0` also covers an explicit "minValue"/"maxValue": null from
        # the API — .get()'s default only applies when the key is absent.
        # A None/dropped default for minValue is masked by `or 0` the same
        # way — verified empirically. maxValue's default is NOT masked by
        # any `or`, so a wrong non-0 default there is real/tested.
        mn = meta.get("minValue", 0) or 0
        mx = meta.get("maxValue", 0) or 0
        if mn == mx:
            continue  # degenerate range
        # display_title's None/dropped default is unobservable: clean_string()
        # below falls back to f'Point {point_id}' regardless. Verified
        # empirically.
        title = point_data.get("display_title") or point_data.get("title", f"Point {point_id}")
        title = clean_string(title) or f"Point {point_id}"
        unit = clean_unit(meta.get("unit"))
        # `or 1` masks a None/dropped default the same way `or 0` masks
        # minValue/maxValue above — but divisor's own default (1) and the
        # `or` fallback value ARE both real/tested, since 1 (default) and
        # a wrong-but-truthy fallback are never masked. Verified empirically.
        div = meta.get("divisor", 1) or 1
        rng = f"{mn / div:g} – {mx / div:g}{' ' + unit if unit else ''}"
        entry = (point_id, title, rng)

        if meta.get("isWritable") and reg == "MODBUS_HOLDING_REGISTER":
            # Check if it's part of a repetitive series
            is_grouped = any(re.search(pat, title, re.IGNORECASE) for pat in _GROUP_PATTERNS)
            if is_grouped:
                unplaced_grouped.append(entry)
            else:
                unplaced_writable.append(entry)
        elif reg in ("MODBUS_HOLDING_REGISTER", "MODBUS_INPUT_REGISTER"):
            unplaced_readonly.append(entry)

    if not unplaced_writable and not unplaced_grouped and not unplaced_readonly:
        return None

    cards = []

    cards.append(
        {
            "type": "markdown",
            "content": (
                "<h2><font color='#9C1924'>Unplaced settings (debug)</font></h2>\n\n"
                "Firmware points present in the bulk fetch but not yet documented "
                "in `menu_structure.yaml`. This tab is only visible in debug mode.\n\n"
                f"**{len(unplaced_writable)} writable (review)** · "
                f"**{len(unplaced_grouped)} writable (series/grouped)** · "
                f"**{len(unplaced_readonly)} read-only**"
            ),
        }
    )

    def _section_rows(entries: list[tuple], label: str) -> list:
        rows = [{"type": "section", "label": label}]
        for point_id, title, rng in entries:
            # A None/dropped default is unobservable — only ever checked
            # via `if default_str:`, where None and '' are both falsy.
            # Verified empirically.
            default_str = point_defaults.get(point_id, "")
            divider = f"{title}  ·  {rng}"
            if default_str:
                divider += f"  ·  default: {default_str}"
            rows.append({"type": "section", "label": divider})
            entity_id = registry_watcher.entity_id_for(point_id)
            if entity_id:
                rows.append({"entity": entity_id})
            else:
                rows.append({"type": "section", "label": f"↳ not enabled (point {point_id})"})
        return rows

    if unplaced_writable:
        rows = _section_rows(
            unplaced_writable, f"Writable — review for YAML ({len(unplaced_writable)} points)"
        )
        cards.append({"type": "entities", "entities": rows})  # type: ignore[dict-item]

    if unplaced_grouped:
        rows = _section_rows(
            unplaced_grouped, f"Writable — multi-system series ({len(unplaced_grouped)} points)"
        )
        cards.append({"type": "entities", "entities": rows})  # type: ignore[dict-item]

    if unplaced_readonly:
        rows = _section_rows(unplaced_readonly, f"Read-only ({len(unplaced_readonly)} points)")
        cards.append({"type": "entities", "entities": rows})  # type: ignore[dict-item]

    return {
        "title": "⚙ Unplaced (debug)",
        "path": "menu-unplaced-debug",
        "cards": [{"type": "vertical-stack", "cards": cards}],
    }


def _build_menu_dashboard_config(
    menu_structure: list,
    registry_watcher: "HAEntityRegistryWatcher",
    known_dynamic: set[int] | None = None,
    point_defaults: dict[int, str] | None = None,
    dynamic_injection: dict[int, list[tuple[str, str, str, str]]] | None = None,
    debug_mode: bool = False,
    bulk_data: dict[int, dict] | None = None,
    menu_yaml_points: set[int] | None = None,
    controller_family: str | None = None,
) -> dict | None:
    """Build the full Lovelace dashboard config for the menu views.

    Each top-level menu becomes a separate view (tab) containing a
    vertical-stack card with interleaved markdown and entities cards.

    controller_family : "air_water" or "water_water" (see
                     _detect_controller_family), or None to skip filtering
                     entirely (shows every menu regardless of family — used
                     when the connected model couldn't be determined).
                     Menus/submenus tagged with a `family` key that doesn't
                     match are dropped from the dashboard before rendering —
                     see _filter_menu_structure_by_family. This only affects
                     what appears in this dashboard; it does not change
                     which points/entities EntityManager enables.
    """
    if controller_family is not None:
        menu_structure = _filter_menu_structure_by_family(menu_structure, controller_family)

    views = []
    included_menus = []
    view_specs = _build_view_specs(menu_structure)
    valid_top_level_menus = {str(spec["menu"]["id"]) for spec in view_specs}
    changed_from_default = (
        _build_changed_from_default(bulk_data, point_defaults or {}) if bulk_data else set()
    )

    for spec in view_specs:
        menu = spec["menu"]
        children = spec["children"]
        cards = _build_menu_view(
            menu,
            registry_watcher,
            known_dynamic or set(),
            point_defaults or {},
            dynamic_injection or {},
            valid_top_level_menus,
            changed_from_default,
            render_submenus=children is None,
        )
        if children is not None:
            # Header markdown card is always cards[0] — see _render_section.
            cards.insert(1, _build_hub_children_card(children))
        if not cards:
            continue

        included_menus.append(menu)

        # No icon — HA shows either icon OR title in the tab bar, not both
        # (without the user enabling a per-dashboard UI toggle). Title is
        # more useful on mobile so we omit the icon entirely.
        view: dict[str, Any] = {
            "title": f"{menu['id']} {menu['title']}",
            "path": f"menu-{str(menu['id']).replace('.', '-')}",
            "cards": _group_cards_for_masonry(cards),
        }
        if spec["subview"]:
            # Requires HA 2024.8+ (Lovelace "subview" support). A subview
            # is left out of the dashboard's top tab strip and instead
            # shows a native back-arrow in its header, using browser-back
            # navigation — reached only via the hub's own links card
            # (_build_hub_children_card) rather than doubling up as its own
            # tab too.
            view["subview"] = True
        views.append(view)

    if not views:
        return None

    views.insert(0, _build_overview_view(included_menus))

    # Debug-only: append unplaced settings view
    if debug_mode and bulk_data and menu_yaml_points is not None:
        unplaced_view = _build_unplaced_view(
            bulk_data,
            menu_yaml_points,
            registry_watcher,
            point_defaults or {},
        )
        if unplaced_view:
            views.append(unplaced_view)

    return {"views": views}


_AIR_WATER_MODEL_PREFIXES = (
    "SMO S40",
    "VVM S310",
    "VVM S320",
    "VVM S325",
    "VVM S330",
    "VVM S500",
    "SVM S332",
)


def _detect_controller_family(model_name: str | None) -> str:
    """Classify the connected controller as "air_water" or "water_water"
    (ground-source, brine-circuit models with a built-in controller), from
    the product model name the REST API reports (device_info["model"], set
    from the API's own product.name field — see _build_device_info).

    This is the signal _filter_menu_structure_by_family uses to drop
    ground-source-only submenus (GP1 pump, brine circuit) from an
    air/water installation's dashboard, and vice versa.

    The recognised air/water prefixes are exactly DOCS.md's own "Indoor
    units / controllers" group (SMO S40, VVM S310/320/325/330/500, SVM
    S332) — confirmed directly against those models' own installer
    manuals, not a guess: VVM S320 and VVM S330's own menu 4.11 overviews
    both list "Fan de-icing" (menu 4.11.3), matching SMO S40 exactly,
    which single-model matching used to hide from them incorrectly.
    Everything else — ground-source models (S1155/S1156/S1255/S1256/
    S1157/S1257), S735/S735C, and any unrecognised or empty name —
    defaults to "water_water". This still fails in the safer direction
    for names outside this list: an unrecognised model just picks up a
    handful of harmless extra tabs, rather than a real ground-source
    installation silently losing GP1/brine controls because its exact
    model string wasn't recognised.
    """
    if model_name and model_name.upper().startswith(_AIR_WATER_MODEL_PREFIXES):
        return "air_water"
    return "water_water"


def _filter_menu_structure_by_family(menus: list, controller_family: str) -> list:
    """Recursively drop any menu/submenu whose `family` tag doesn't match
    controller_family, keeping everything else (including menus with no
    `family` key at all, which apply to every installation).

    Returns a new list — the input menu_structure is never mutated, since
    callers may reuse the same in-memory structure across dashboard
    rebuilds and for other purposes (e.g. _collect_menu_points).
    """
    filtered = []
    for m in menus:
        menu_family = m.get("family")
        if menu_family is not None and menu_family != controller_family:
            continue
        m = dict(m)
        if "submenus" in m:
            m["submenus"] = _filter_menu_structure_by_family(m["submenus"], controller_family)
        filtered.append(m)
    return filtered


def _collect_menu_points(menus: list) -> set[int]:
    """Walk the full menu hierarchy and collect every point_id referenced
    in any setting, at any nesting depth. Module-level (not a nested
    closure) so this pure recursive logic can be unit tested directly
    against hand-built menu structures, independent of the real
    menu_structure.yaml or any WebSocket/registry dependency."""
    pids: set[int] = set()
    for m in menus:
        for s in m.get("settings", []):
            pid = s.get("point_id")
            # is not None, not a truthy check: the schema's documented
            # label-only-row sentinel is point_id: null, not point_id: 0 —
            # a real (if currently unseen) firmware variableId of 0 must
            # not be silently dropped and treated the same as "no point".
            if pid is not None:
                pids.add(pid)
        pids.update(_collect_menu_points(m.get("submenus", [])))
    return pids


def _build_point_to_menu(menus: list, result: dict | None = None) -> dict:
    """Build the reverse lookup point_id -> (menu_id, menu_title) by
    walking the full menu hierarchy. Module-level for the same testability
    reason as _collect_menu_points above."""
    if result is None:
        result = {}
    for m in menus:
        mid = m.get("id", "")
        title = m.get("title", "")
        for s in m.get("settings", []):
            pid = s.get("point_id")
            # is not None, not a truthy check — see _collect_menu_points.
            if pid is not None:
                result[pid] = (mid, title)
        _build_point_to_menu(m.get("submenus", []), result)
    return result


def _should_attempt_dashboard_create(dashboards_response: dict, slug: str) -> bool:
    """Decide whether to call lovelace/dashboards/create, given the response
    to a prior lovelace/dashboards list call.

    Returns True only when the list call genuinely succeeded AND no
    dashboard with the given url_path was found. A failed or empty-but-
    unsuccessful list response (e.g. _ws_call returning {} after a dead
    WebSocket) must NOT be treated as "no dashboards exist" — doing so
    causes a doomed create attempt on every retry, which Home Assistant
    logs as a recurring "URL already in use" error even though the
    dashboard genuinely already exists from a prior run.

    Module-level and side-effect-free so this specific decision can be
    unit tested directly, independent of the rest of _setup_menu_dashboard.
    """
    if not dashboards_response.get("success"):
        return False
    existing = next(
        (d for d in dashboards_response.get("result", []) if d.get("url_path") == slug),
        None,
    )
    return existing is None


def _wait_for_registry_stable(
    registry_watcher: "HAEntityRegistryWatcher",
    available_menu_points: set,
    active_dynamic: set,
) -> None:
    """Poll until the HA entity registry resolves entity IDs for both:

    1. All available menu points (needed on every startup — without this
       the _unique_id_map is empty and all entities show as "not enabled")
    2. All active dynamic points (needed for injection after a controlling
       point is flipped — these arrive via registry create events)

    Polls until BOTH sets are stable, up to a 60s limit. Dynamic points get
    a shorter inner timeout (8s once menu points are stable) so a single
    disconnected accessory doesn't block indefinitely.
    """
    _step = 0.5
    _limit = 60.0
    _waited = 0.0
    # Any sentinel (negative or None) works here — only ever compared
    # with `==` to a real (non-negative) entity count, never read as an
    # actual count value. Verified empirically.
    _prev_count = -1
    # A wrong initial value (None, or a truthy float like 1.0) is also
    # unobservable — no test asserts the exact number of loop iterations
    # needed to reach _stable_need. Verified empirically.
    _stable_for = 0.0
    _stable_need = 3.0
    # On a fresh start HA creates entities in batches, causing the count to
    # pause between waves — the stability check fires during a gap and exits
    # prematurely with only a fraction of entities resolved.  Require at least
    # 70% of expected menu points before accepting a stable count as "done".
    # This threshold tolerates genuinely absent conditional points (e.g. 3671,
    # 5033 absent when a room sensor is installed) and a modest HA indexing lag
    # without waiting the full 60s limit. 80% was too high in practice — on a
    # typical 280-point menu install only ~205 (~73%) resolve within the wait
    # window on a fresh mode-change restart.
    _completeness_threshold = 0.70

    while _waited < _limit:
        time.sleep(_step)
        _waited += _step

        menu_resolved = sum(1 for p in available_menu_points if registry_watcher.entity_id_for(p))
        dyn_resolved = sum(1 for p in active_dynamic if registry_watcher.entity_id_for(p))
        current_count = menu_resolved + dyn_resolved

        if current_count == _prev_count:
            _stable_for += _step
            # Don't accept stability if we're well below the expected count —
            # on a fresh start HA creates entities in waves and the count may
            # pause between waves, producing a false stable window.
            menu_complete = menu_resolved >= len(available_menu_points) * _completeness_threshold
            if _stable_for >= _stable_need and menu_resolved > 0 and menu_complete:
                # All dynamic points resolved — ideal exit
                if dyn_resolved == len(active_dynamic):
                    log_startup.debug(
                        "Registry stable: %d/%d menu + %d/%d dynamic after %.1fs",
                        menu_resolved,
                        len(available_menu_points),
                        dyn_resolved,
                        len(active_dynamic),
                        _waited,
                    )
                    return
                # Menu stable but dynamic still missing — wait a bit more
                # but don't hold up the retry mechanism indefinitely
                if _stable_for >= 8.0:
                    # This call is unobservable — the test that exercises
                    # this branch (test_eight_second_fallback_boundary_uses_
                    # greater_or_equal_eight) only counts loop iterations via
                    # a mocked time.sleep, it never captures/formats this
                    # log record. Verified empirically. (Contrast with the
                    # sibling "Registry stable: ..." log a few lines above,
                    # which IS format-tested.)
                    log_startup.debug(
                        "Registry stable at %d/%d menu, %d/%d dynamic after %.1fs — proceeding",
                        menu_resolved,
                        len(available_menu_points),
                        dyn_resolved,
                        len(active_dynamic),
                        _waited,
                    )  # pragma: no mutate
                    return
        else:
            _stable_for = 0.0
            _prev_count = current_count
    log_startup.warning(
        "Registry wait timed out — %d/%d menu + %d/%d dynamic resolved",
        sum(1 for p in available_menu_points if registry_watcher.entity_id_for(p)),
        len(available_menu_points),
        sum(1 for p in active_dynamic if registry_watcher.entity_id_for(p)),
        len(active_dynamic),
    )


def _setup_menu_dashboard(
    open_ws_fn: Callable, registry_watcher: "HAEntityRegistryWatcher", debug_mode: bool = False
) -> bool:
    """Build and save the Nibe Menus Lovelace dashboard config.

    Always rebuilds and saves the full config on every call — ensures
    state values are always current after any startup or restart.
    Only skips the dashboard creation call if the dashboard already
    exists in HA (creation is a one-time WebSocket operation).

    Takes open_ws_fn (callable → (ws, next_id) | None) rather than a
    pre-opened ws, because the registry wait below can take up to 60s.
    A WebSocket opened before the wait would be closed by the Supervisor
    as idle before the Lovelace calls that follow it. Opening it after
    the wait (immediately before the Lovelace API calls) keeps the
    connection fresh.

    Returns True if a retry is needed (active dynamic points not yet in
    the HA entity registry), False otherwise.
    """

    # Menu points are enabled by EntityManager.apply_mode() before this
    # function ever runs (menus mode only) — this function is purely a
    # dashboard builder. It still needs the menu point set below to know
    # which entities to wait on and reference while building the config.
    entity_manager = registry_watcher._em

    # Load menu structure first so we know which points to enable
    menu_path = os.path.join(os.path.dirname(__file__), "menu_structure.yaml")
    if not os.path.exists(menu_path):
        log_startup.debug("menu_structure.yaml not found — skipping menu dashboard")
        return False

    try:
        menu_structure = _load_menu_structure_yaml(menu_path)
    except (OSError, ValueError, yaml.YAMLError, AttributeError) as e:
        # OSError: file missing/unreadable. ValueError: e.g. an embedded
        # null byte in the path, which open() rejects before it can even
        # raise OSError. yaml.YAMLError: malformed YAML. AttributeError:
        # top-level YAML document isn't a mapping.
        log_startup.warning("Could not load menu_structure.yaml: %s", e)
        return False

    if not menu_structure:
        log_startup.debug("menu_structure.yaml has no menus — skipping")
        return False

    all_menu_points = _collect_menu_points(menu_structure)

    # Build reverse lookup: point_id → (menu_id, menu_title)
    # Walk the full hierarchy so nested menus are covered.
    entity_manager.point_to_menu_map = _build_point_to_menu(menu_structure)
    # Points actually present in the bulk data. In menus mode these were
    # already enabled by EntityManager.apply_mode() before this function
    # runs (see generate_nibe_mqtt.py's startup sequence) — this function
    # only builds the dashboard, it no longer enables anything itself.
    available_menu_points = {
        pid for pid in all_menu_points if pid in entity_manager.all_points_by_id
    }

    # Wait for the registry watcher to resolve entity IDs for both available
    # menu points and active dynamic points before building the dashboard.
    active_dynamic = entity_manager.active_dynamic_points
    _wait_for_registry_stable(registry_watcher, available_menu_points, active_dynamic)

    # Build dashboard config
    # all_points_by_id is mutated under _em_lock by _index_point/_deindex_point
    # (e.g. from _publish_dynamic_changes when new dynamic points appear), so
    # taking the lock here does serialize against that writer. But per the
    # _em_lock caveat in EntityManager.__init__, _fetch_bulk_data also mutates
    # all_points_by_id and bulk_data directly from the poll thread WITHOUT
    # acquiring this lock — so the lock is real but partial protection: it
    # closes the race against _index_point-based writers, not against
    # _fetch_bulk_data. The pair of snapshots can still be mutually
    # inconsistent if a poll cycle's unlocked mutation lands between the two
    # dict() copies below; that residual, bounded staleness is accepted for a
    # dashboard rebuild rather than a correctness guarantee.
    with entity_manager._em_lock:
        all_points_snapshot = dict(entity_manager.all_points_by_id)
        bulk_data_snapshot = dict(entity_manager.bulk_data)

    known_dynamic = entity_manager.dynamic_point_map.all_known_dynamic_point_ids()
    point_defaults = _build_point_defaults(all_points_snapshot)
    dynamic_injection = _build_dynamic_injection(
        entity_manager.dynamic_point_map,
        entity_manager.active_dynamic_points,
        registry_watcher,
        all_points_snapshot,
        point_defaults,
    )
    controller_family = _detect_controller_family(entity_manager.device_info.get("model"))
    dashboard_config = _build_menu_dashboard_config(
        menu_structure,
        registry_watcher,
        known_dynamic,
        point_defaults,
        dynamic_injection,
        debug_mode=debug_mode,
        bulk_data=bulk_data_snapshot,
        menu_yaml_points=all_menu_points,
        controller_family=controller_family,
    )
    if not dashboard_config or not dashboard_config.get("views"):
        log_startup.warning("Menu dashboard: no views generated — check menu_structure.yaml")
        return False

    # Open a fresh WebSocket NOW — after the registry wait — so the connection
    # is live when the Lovelace API calls below use it. Opening it before the
    # wait caused the Supervisor to close it as idle during the wait period,
    # resulting in every subsequent _ws_call returning {} ("returned no result").
    ws_result = open_ws_fn()
    if ws_result is None:
        log_startup.warning("Menu dashboard: could not open WebSocket for Lovelace API calls")
        return True  # signal retry
    ws, next_id = ws_result

    try:
        return _setup_menu_dashboard_lovelace(
            ws,
            next_id,
            dashboard_config,
            entity_manager,
            registry_watcher,
            available_menu_points,
            active_dynamic,
        )
    finally:
        try:  # noqa: SIM105 — deliberately broad, documented on the except line below
            ws.close()
        except Exception:  # noqa: BLE001, S110 — best-effort ws.close() during cleanup; primary error already logged  # nosec B110
            pass


def _setup_menu_dashboard_lovelace(
    ws: Any,
    next_id: Callable[[], int],
    dashboard_config: dict,
    entity_manager: "EntityManager",
    registry_watcher: "HAEntityRegistryWatcher",
    available_menu_points: set,
    active_dynamic: set,
) -> bool:
    """Perform the Lovelace API calls for the menu dashboard.

    Separated from _setup_menu_dashboard so it can run on a freshly-opened
    WebSocket (after the registry wait) rather than one opened before the
    wait that may have been closed by the Supervisor as idle.

    Returns True if a retry is needed, False if the dashboard was saved
    successfully (or failed fatally and should not be retried).
    """
    dashboards = _ws_call(ws, next_id(), {"type": "lovelace/dashboards/list"})
    if _should_attempt_dashboard_create(dashboards, _MENU_DASHBOARD_SLUG):
        resp = _ws_call(
            ws,
            next_id(),
            {
                "type": "lovelace/dashboards/create",
                "url_path": _MENU_DASHBOARD_SLUG,
                "mode": "storage",
                "title": _MENU_DASHBOARD_TITLE,
                "icon": "mdi:book-open-outline",
                "show_in_sidebar": True,
            },
        )
        if not resp.get("success"):
            # A missing 'error' key defaulting to None instead of {} is
            # unobservable: str(None) == "None" and str({}) == "{}" both
            # lack the sentinel substrings checked below, so either default
            # takes the same "genuine failure" branch.
            error_msg = str(resp.get("error", {}))
            if "url_already_exists" not in error_msg and "already in use" not in error_msg:
                log_startup.warning("Could not create Nibe Menus dashboard: %s", resp)
                return False
            log_startup.debug("Nibe Menus dashboard already exists — proceeding to update config")
    elif not dashboards.get("success"):
        log_startup.warning("lovelace/dashboards/list call returned no result — will retry.")
        return True
    else:
        log_startup.debug("Nibe Menus dashboard already exists — skipping create")

    # Save the dashboard config (views + cards)
    resp = _ws_call(
        ws,
        next_id(),
        {
            "type": "lovelace/config/save",
            "url_path": _MENU_DASHBOARD_SLUG,
            "config": dashboard_config,
        },
    )

    log_startup.debug("lovelace/config/save response: %s", resp)
    if resp.get("success"):
        view_count = len(dashboard_config["views"])
        log_startup.info(
            "Nibe Menus dashboard provisioned with %d menu view(s). "
            "Find it in your HA sidebar under '%s'.",
            view_count,
            _MENU_DASHBOARD_TITLE,
        )
        # Fire lovelace_updated event so connected browsers reload.
        _ws_call(
            ws,
            next_id(),
            {
                "type": "fire_event",
                "event_type": "lovelace_updated",
                "event_data": {"url_path": _MENU_DASHBOARD_SLUG},
            },
        )

        # Verify all active dynamic points are in the registry.
        # Only retry if menu entities resolved correctly (registry is up)
        # but dynamic point(s) still missing.
        # menu_resolved is only ever compared against 0 below, so the
        # per-point weight (1) is unobservable — any positive constant
        # would behave identically.
        menu_resolved = sum(1 for p in available_menu_points if registry_watcher.entity_id_for(p))
        missing_dynamic = [
            p for p in entity_manager.active_dynamic_points if not registry_watcher.entity_id_for(p)
        ]
        if missing_dynamic and menu_resolved > 0:
            log_startup.debug(
                "Dashboard saved but %d dynamic point(s) not yet in registry — retry needed: %s",
                len(missing_dynamic),
                missing_dynamic,
            )
            return True  # needs retry
        return False  # all good
    else:
        log_startup.warning("Menu dashboard config save failed: %s", resp)
        return False


def _open_ha_websocket() -> tuple[Any, Callable[[], int]] | None:
    """Open and authenticate a WebSocket connection to the HA Supervisor.

    Returns (ws, next_id_callable) on success, or None if the connection
    cannot be established (no token, import error, auth failure).
    """
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
    if not supervisor_token:
        return None

    try:
        import websocket

        ws = websocket.create_connection("ws://supervisor/core/websocket", timeout=10)
    except ImportError:
        log_startup.warning("websocket-client not installed — WebSocket unavailable")
        return None
    except Exception as e:  # noqa: BLE001 — best-effort I/O/network op; logged and degrades gracefully
        log_startup.warning("Could not connect to HA WebSocket: %s", e)
        return None

    _mid: int = 0

    def _next_id() -> int:
        nonlocal _mid
        _mid += 1
        return _mid

    try:
        greeting = json.loads(ws.recv())
        if greeting.get("type") != "auth_required":
            log_startup.warning("Unexpected HA WebSocket greeting: %s", greeting.get("type"))
            ws.close()
            return None
        ws.send(json.dumps({"type": "auth", "access_token": supervisor_token}))
        auth_result = json.loads(ws.recv())
        if auth_result.get("type") != "auth_ok":
            log_startup.warning("HA WebSocket auth failed")
            ws.close()
            return None
        return ws, _next_id
    except Exception as e:  # noqa: BLE001 — best-effort I/O/network op; logged and degrades gracefully
        log_startup.warning("HA WebSocket auth error: %s", e)
        try:  # noqa: SIM105 — deliberately broad, documented on the except line below
            ws.close()
        except Exception:  # noqa: BLE001, S110 — best-effort ws.close() during cleanup; primary error already logged  # nosec B110
            pass
        return None


def _setup_lovelace(
    version: str,
    device_name: str,
    registry_watcher: "HAEntityRegistryWatcher | None" = None,
    debug_mode: bool = False,
    mode: str = "menus",
) -> None:
    """Register the card JS resource and provision the Nibe Bridge dashboard.

    Opens a single WebSocket connection to the HA supervisor for steps 1–2.
      1. Resource registration / update (versioned URL with content hash).
      2. Main dashboard provisioning.
      3. Menu dashboard build and save — menus mode only, via
         _regen_menu_dashboard (its own independent WebSocket connection,
         since it must be able to retry after this function returns — a
         bare single-attempt call here has no way to retry if the entity
         registry hasn't caught up yet, e.g. right after a mode change
         enabled a large batch of points). In any other mode no menu
         points are enabled and there is nothing for it to render; the
         caller (generate_nibe_mqtt.py) is responsible for tearing down a
         menu dashboard left over from a previous menus-mode run via
         remove_menu_dashboard().

    Steps 1–2 (card resource + Bridge dashboard) run in every mode — they
    are the management surface and remain the only way to enable entities
    in "none" mode.

    Safe to call on every startup — all operations are idempotent.
    No-op when running outside the HA add-on environment (no SUPERVISOR_TOKEN).

    Default mode="menus" preserves prior behaviour for any caller (tests
    included) that doesn't pass mode explicitly.
    """
    _FLAG_FILE = _LOVELACE_FLAG

    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
    if not supervisor_token:
        log_startup.debug(
            "No SUPERVISOR_TOKEN — skipping Lovelace setup (running outside HA add-on environment)"
        )
        return

    # ── Build versioned resource URL ──────────────────────────────────────────
    card_path = "/app/nibe-entity-manager-card.js"
    try:
        with open(card_path, "rb") as f:
            cache_key = hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError:
        cache_key = version
    versioned_url = f"/local/nibe-entity-manager-card.js?v={cache_key}"

    # ── Open WebSocket and authenticate ─────────────────────────────────────────
    result = _open_ha_websocket()
    if result is None:
        log_startup.debug(
            "Could not open HA WebSocket — Lovelace setup skipped "
            "(no SUPERVISOR_TOKEN or connection failed)"
        )
        return
    ws, _next_id = result

    try:
        # ── Step 1: Resource registration ─────────────────────────────────────
        _setup_lovelace_resource(ws, _next_id, versioned_url)

        # ── Step 2: Dashboard provisioning ────────────────────────────────────
        _setup_lovelace_dashboard(ws, _next_id, device_name, _FLAG_FILE)

        # ── Step 3: Menu dashboard provisioning — menus mode only ────────────────
        # Uses _regen_menu_dashboard (not a bare _setup_menu_dashboard call) so
        # the initial startup build gets the same retry/backoff coverage as a
        # later regen. A large batch of newly enabled points (e.g. a mode
        # change into "menus") is exactly the case where the registry needs
        # more than one 60s wait window to catch up — a bare single-attempt
        # call here previously had no way to retry, so the dashboard would
        # simply never appear if that first attempt didn't finish in time.
        # This opens its own WebSocket connection (independent of ws/_next_id
        # above) since it must be able to retry after this function returns.
        if registry_watcher is not None and mode == "menus":
            _regen_menu_dashboard(registry_watcher, debug_mode, attempt=1)

    except Exception as e:  # noqa: BLE001 — best-effort I/O/network op; logged and degrades gracefully
        log_startup.warning("Lovelace setup failed: %s", e)
    finally:
        try:  # noqa: SIM105 — deliberately broad, documented on the except line below
            ws.close()
        except Exception:  # noqa: BLE001, S110 — best-effort ws.close() during cleanup; primary error already logged  # nosec B110
            pass


def _setup_lovelace_resource(ws: Any, next_id: Callable[[], int], versioned_url: str) -> None:
    """Register or update the card JS file as a Lovelace module resource.

    Called from _setup_lovelace with an already-authenticated WebSocket.
    """
    # ws/next_id/result-default mutations throughout this function are
    # unobservable — this function's own test suite's fake_ws_call never
    # inspects its ws or msg_id arguments (only payload), and always
    # returns a dict with a 'result' key so the default never fires. A
    # wrong non-matching default for r.get("url", "") below is also
    # unobservable — the substring check fails identically for any
    # non-matching string. Verified empirically.
    resp = _ws_call(ws, next_id(), {"type": "lovelace/resources/list"})
    resources = resp.get("result", [])

    # Find all existing registrations for this card (duplicates cause
    # "already defined" errors in Safari when two versions are loaded).
    matching = [r for r in resources if "nibe-entity-manager-card.js" in r.get("url", "")]

    # Delete any duplicates beyond the first
    for dup in matching[1:]:
        _ws_call(
            ws,
            next_id(),
            {
                "type": "lovelace/resources/delete",
                "resource_id": dup.get("id"),
            },
        )
        log_startup.info("Removed duplicate Lovelace resource: %s", dup.get("url"))

    existing = matching[0] if matching else None

    if existing is not None:
        if existing.get("url") == versioned_url:
            log_startup.debug(
                "Lovelace resource already current (%s) — no update needed", versioned_url
            )
            return
        resp = _ws_call(
            ws,
            next_id(),
            {
                "type": "lovelace/resources/update",
                "resource_id": existing.get("id"),
                "res_type": "module",
                "url": versioned_url,
            },
        )
        action = "Updated"
    else:
        resp = _ws_call(
            ws,
            next_id(),
            {
                "type": "lovelace/resources/create",
                "res_type": "module",
                "url": versioned_url,
            },
        )
        action = "Registered"

    if resp.get("success"):
        log_startup.info("%s Lovelace resource: %s", action, versioned_url)
    else:
        log_startup.warning("Lovelace resource %s failed: %s", action.lower(), resp)


def _setup_lovelace_dashboard(
    ws: Any, next_id: Callable[[], int], device_name: str, flag_file: str
) -> None:
    """Create the Nibe Bridge dashboard if it does not already exist.

    Called from _setup_lovelace with an already-authenticated WebSocket.

    Idempotent — skips the create call entirely if flag_file exists, which
    prevents HA from logging a spurious error on every restart. The flag is
    written after successful creation or when HA reports the slug is already
    in use (meaning the dashboard exists from a previous run).
    """
    if os.path.exists(flag_file):
        log_startup.debug("Nibe Bridge dashboard already provisioned — skipping")
        return

    # ws->None mutations on every _ws_call() below in this function are
    # unobservable: this function's own test suite (TestSetupLovelaceDashboard)
    # patches _ws_call with a fake that never inspects its ws argument
    # (only payload). Verified empirically. (A ws->None mutation IS caught
    # elsewhere in the module by a cross-cutting test that checks every
    # _ws_call site receives the real ws — but that test does not exercise
    # this specific function's call sites.)
    # Check if dashboard already exists before attempting create — avoids HA
    # logging a system-log error for "URL already in use" on every restart
    # after a container rebuild that wiped the flag file.
    dashboards = _ws_call(ws, next_id(), {"type": "lovelace/dashboards/list"})
    if not _should_attempt_dashboard_create(dashboards, _DASHBOARD_SLUG):
        if not dashboards.get("success"):
            # Genuine list failure (e.g. _ws_call returning {} after a dead
            # WebSocket) — do NOT write the flag file here. Unlike the
            # "dashboard confirmed to exist" case below, we don't actually
            # know whether it exists; writing the flag would permanently
            # skip creation on a fresh install if this happened to fail on
            # the very first startup. Leave flag_file absent so this is
            # retried on the next restart, matching the menu dashboard
            # path's equivalent branch.
            log_startup.warning(
                "lovelace/dashboards/list call failed — will retry creating "
                "the Nibe Bridge dashboard on next restart."
            )
            return
        log_startup.info(
            "Nibe Bridge dashboard already exists (/%s) — writing flag to skip future attempts",
            _DASHBOARD_SLUG,
        )
        try:
            with open(flag_file, "w") as f:
                f.write("provisioned\n")
        except OSError as e:
            log_startup.warning("Could not write lovelace provisioned flag: %s", e)
        return

    resp = _ws_call(
        ws,
        next_id(),
        {
            "type": "lovelace/dashboards/create",
            "url_path": _DASHBOARD_SLUG,
            "mode": "storage",
            "title": _DASHBOARD_TITLE,
            "icon": _DASHBOARD_ICON,
            "show_in_sidebar": True,
            "require_admin": False,
        },
    )

    if not resp.get("success"):
        error = resp.get("error", {})
        # error_code is only ever compared against the "url_already_exists"
        # string sentinel, so a "" vs None default for the missing 'code'
        # key is unobservable — neither equals the sentinel.
        error_code = error.get("translation_key") or error.get("code", "")
        # A wrong non-empty default (e.g. "XXXX") is also unobservable —
        # only ever checked via `"already in use" in error_msg.lower()`,
        # which any non-matching default string fails identically.
        # Verified empirically.
        error_msg = error.get("message", "")
        if error_code == "url_already_exists" or "already in use" in error_msg.lower():
            log_startup.info(
                "Nibe Bridge dashboard already exists (/%s) — writing flag to skip future attempts",
                _DASHBOARD_SLUG,
            )
            try:
                with open(flag_file, "w") as f:
                    f.write("provisioned\n")
            except OSError as e:
                # e/format-string mutations here are log-only. Verified empirically.
                log_startup.warning(
                    "Could not write lovelace provisioned flag: %s", e
                )  # pragma: no mutate
        else:
            log_startup.warning("Could not create Nibe Bridge dashboard: %s", resp)
        return

    dashboard_id = resp.get("result", {}).get("id")
    log_startup.info(
        "Created Nibe Bridge dashboard (id=%s, url_path=/%s)",
        dashboard_id,
        _DASHBOARD_SLUG,
    )

    view_title = device_name
    dashboard_config = {
        "views": [
            {
                "title": view_title,
                "path": "home",
                "icon": _DASHBOARD_ICON,
                "type": "panel",
                "cards": [
                    {
                        "type": _CARD_TYPE,
                        "title": "",
                        "pageSize": 50,
                        "suppressInitialToasts": True,
                    }
                ],
            }
        ]
    }

    resp = _ws_call(
        ws,
        next_id(),
        {
            "type": "lovelace/config/save",
            "url_path": _DASHBOARD_SLUG,
            "config": dashboard_config,
        },
    )

    if resp.get("success"):
        log_startup.info(
            "Nibe Bridge dashboard configured with '%s' card. "
            "Find it in your HA sidebar under '%s'.",
            _CARD_TYPE,
            _DASHBOARD_TITLE,
        )
        try:
            with open(flag_file, "w") as f:
                f.write("provisioned\n")
        except OSError as e:
            # e/format-string mutations here are log-only. Verified empirically.
            log_startup.warning(
                "Could not write lovelace provisioned flag: %s", e
            )  # pragma: no mutate
    else:
        log_startup.warning(
            "Dashboard created but card config could not be written: %s. "
            "You can add the '%s' card manually.",
            resp,
            _CARD_TYPE,
        )


def _ws_call(ws: Any, msg_id: int, payload: dict, timeout: int = 10) -> dict:
    """Send a single WebSocket message and return the parsed response.

    Attaches the message ID, sends, and reads messages until it finds the
    result matching msg_id.  Intermediate event messages (e.g. from active
    subscriptions) are discarded so they do not corrupt command/response pairs.
    Returns an empty dict on timeout or error, including if the connection
    has already failed (e.g. BrokenPipeError on send) — callers should treat
    an empty dict as "this call did not succeed" regardless of cause.

    ``timeout`` is an overall budget for the whole call, not a per-recv
    budget: the socket timeout is recomputed from the remaining time before
    every recv(), so a stream of interleaved irrelevant messages (each
    arriving just under its own recv timeout) can't extend the total wait
    past ``timeout`` seconds — a single ``ws.settimeout(timeout)`` before
    the loop would let each recv() get its own fresh `timeout`-second
    window, regardless of how much wall-clock time earlier iterations
    already used.
    """
    try:
        ws.send(json.dumps({**payload, "id": msg_id}))
    except Exception as e:  # noqa: BLE001 — best-effort I/O/network op; logged and degrades gracefully
        log_startup.debug("_ws_call: send failed (id=%s): %s", msg_id, e)
        return {}
    deadline = time.time() + timeout
    try:
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            ws.settimeout(remaining)
            raw = ws.recv()
            if not raw:
                break
            msg: dict = json.loads(raw)
            if msg.get("id") == msg_id and msg.get("type") == "result":
                return msg
    except Exception as e:  # noqa: BLE001 — best-effort I/O/network op; logged and degrades gracefully
        log_startup.debug("_ws_call: recv failed (id=%s): %s", msg_id, e)
    finally:
        ws.settimeout(None)
    return {}


def _teardown_lovelace(remove_frontend: bool) -> None:
    """Remove the Nibe Bridge dashboard, its Lovelace resource registration,
    and the card file from /homeassistant/www/ on clean shutdown when the
    remove_frontend option is set to true.

    This is intentionally opt-in rather than running on every restart:
    - Normal restarts and add-on updates must NOT touch the dashboard.
    - Only a deliberate uninstall / data-removal flow should clean up.

    All steps are individually guarded — partial failures are logged as
    warnings so a broken WebSocket connection does not prevent the card
    file from being removed (or vice versa).
    """
    if not remove_frontend:
        return

    log_startup.info("remove_frontend=true — removing Lovelace dashboard and resources")

    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")

    # ── Remove card file from /homeassistant/www/ ────────────────────────────
    card_dst = "/homeassistant/www/nibe-entity-manager-card.js"
    try:
        if os.path.exists(card_dst):
            os.remove(card_dst)
            log_startup.info("Removed card file: %s", card_dst)
        else:
            log_startup.debug("Card file not found at %s — already removed", card_dst)
    except OSError as e:
        log_startup.warning("Could not remove card file %s: %s", card_dst, e)

    if not supervisor_token:
        log_startup.warning(
            "No SUPERVISOR_TOKEN — cannot remove Lovelace dashboard or resource "
            "(running outside HA add-on environment)"
        )
        return

    # ── Open WebSocket ────────────────────────────────────────────────────────
    result = _open_ha_websocket()
    if result is None:
        log_startup.warning(
            "Could not open HA WebSocket for Lovelace teardown — "
            "dashboard and resource will not be removed"
        )
        return
    ws, _next_id = result

    try:
        try:
            # ws->None and result-default mutations throughout this
            # function are unobservable — this function's own test suite's
            # fake_ws_call never inspects its ws argument (only payload),
            # and always returns a dict with a 'result' key so the default
            # never fires. A wrong non-matching default for r.get("url", "")
            # below is also unobservable — the substring check fails
            # identically for any non-matching string. Verified empirically.
            resp = _ws_call(ws, _next_id(), {"type": "lovelace/dashboards/list"})
            dashboards = resp.get("result", [])
            existing = next(
                (d for d in dashboards if d.get("url_path") == _DASHBOARD_SLUG),
                None,
            )
            if existing is not None:
                resp = _ws_call(
                    ws,
                    _next_id(),
                    {
                        "type": "lovelace/dashboards/delete",
                        "dashboard_id": existing.get("id"),
                    },
                )
                if resp.get("success"):
                    log_startup.info("Removed Nibe Bridge dashboard (id=%s)", existing.get("id"))
                else:
                    log_startup.warning("Could not remove dashboard: %s", resp)
            else:
                log_startup.debug("Nibe Bridge dashboard not found — already removed")
        except Exception as e:  # noqa: BLE001 — best-effort teardown; logged and degrades gracefully
            log_startup.warning("Dashboard removal failed: %s", e)

        # ── Remove Lovelace resource registration ─────────────────────────────
        try:
            resp = _ws_call(ws, _next_id(), {"type": "lovelace/resources/list"})
            resources = resp.get("result", [])
            existing = next(
                (r for r in resources if "nibe-entity-manager-card.js" in r.get("url", "")),
                None,
            )
            if existing is not None:
                resp = _ws_call(
                    ws,
                    _next_id(),
                    {
                        "type": "lovelace/resources/delete",
                        "resource_id": existing.get("id"),
                    },
                )
                if resp.get("success"):
                    log_startup.info("Removed Lovelace resource registration")
                else:
                    log_startup.warning("Could not remove Lovelace resource: %s", resp)
            else:
                log_startup.debug("Lovelace resource not found — already removed")
        except Exception as e:  # noqa: BLE001 — best-effort teardown; logged and degrades gracefully
            log_startup.warning("Resource removal failed: %s", e)

    finally:
        try:  # noqa: SIM105 — deliberately broad, documented on the except line below
            ws.close()
        except Exception:  # noqa: BLE001, S110 — best-effort ws.close() during cleanup; primary error already logged  # nosec B110
            pass

    # Remove the provisioned flag so the dashboard is recreated if the
    # add-on is reinstalled after a clean removal.
    try:
        os.remove(_LOVELACE_FLAG)
        log_startup.debug("Lovelace provisioned flag file removed")
    except OSError:
        pass

    log_startup.info("Lovelace teardown complete")


def _remove_menu_dashboard() -> None:
    """Remove the Nibe Menus dashboard if it exists. Idempotent — a no-op
    when the dashboard is absent.

    Unlike _teardown_lovelace (opt-in, uninstall-only), this runs on every
    startup in a non-menus mode. Leaving the mode disables its points via
    EntityManager.apply_mode(), so an orphaned menu dashboard would show a
    wall of unavailable entities until manually deleted — this keeps that
    self-healing rather than requiring manual cleanup.

    Does not touch the Bridge dashboard, the card resource registration,
    or the card file — those remain provisioned in every mode.
    """
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
    if not supervisor_token:
        log_startup.debug(
            "No SUPERVISOR_TOKEN — skipping menu dashboard teardown "
            "(running outside HA add-on environment)"
        )
        return

    result = _open_ha_websocket()
    if result is None:
        log_startup.debug(
            "Could not open HA WebSocket for menu dashboard teardown — will retry on next startup"
        )
        return
    ws, _next_id = result

    try:
        resp = _ws_call(ws, _next_id(), {"type": "lovelace/dashboards/list"})
        dashboards = resp.get("result", [])
        # Only ever used in truthiness checks below, so a None default is
        # unobservable — both False and None are falsy.
        list_succeeded = resp.get("success", False)
        existing = next(
            (d for d in dashboards if d.get("url_path") == _MENU_DASHBOARD_SLUG),
            None,
        )
        if existing is not None:
            # ws->None is unobservable here — same reasoning as the other
            # _ws_call sites in this file: this function's own test suite's
            # fake_ws_call never inspects its ws argument. Verified
            # empirically.
            resp = _ws_call(
                ws,
                _next_id(),
                {
                    "type": "lovelace/dashboards/delete",
                    "dashboard_id": existing.get("id"),
                },
            )
            if resp.get("success"):
                log_startup.info("Removed Nibe Menus dashboard (id=%s)", existing.get("id"))
            else:
                log_startup.warning("Could not remove Nibe Menus dashboard: %s", resp)
                # False->None is unobservable — list_succeeded is only ever
                # read via `not list_succeeded`/truthiness, where False and
                # None are equally falsy. Verified empirically.
                list_succeeded = False  # suppress flag removal — retry next startup
        elif not list_succeeded:
            log_startup.debug(
                "Nibe Menus dashboard list call returned no result — will retry on next startup"
            )
        else:
            log_startup.debug("Nibe Menus dashboard not found — nothing to remove")
    except Exception as e:  # noqa: BLE001 — best-effort I/O/network op; logged and degrades gracefully
        log_startup.warning("Menu dashboard teardown failed: %s", e)
        # Same False/None-truthiness equivalence as above.
        list_succeeded = False
    finally:
        try:  # noqa: SIM105 — deliberately broad, documented on the except line below
            ws.close()
        except Exception:  # noqa: BLE001, S110 — best-effort ws.close() during cleanup; primary error already logged  # nosec B110
            pass

    # Remove the flag only when we know the list call succeeded — if the call
    # returned {} (stale connection), we don't know whether the dashboard still
    # exists, so keep the flag so the next startup retries.
    if list_succeeded:
        try:
            os.remove(_MENU_DASHBOARD_FLAG)
            log_startup.debug("Menu dashboard provisioned flag file removed")
        except OSError:
            pass


# ============================================================================
# MAIN
# ============================================================================


def _regen_menu_dashboard(
    registry_watcher: "HAEntityRegistryWatcher",
    debug_mode: bool,
    attempt: int = 1,
    max_attempts: int = 3,
    retry_delay: float = 3.0,
    open_ws_fn: Callable | None = None,
    setup_dashboard_fn: Callable | None = None,
    schedule_retry_fn: Callable | None = None,
) -> None:
    """Perform one menu dashboard regeneration attempt, retrying on failure.

    Extracted to module level (rather than a closure inside main()) so the
    retry/exception-handling logic can be unit tested directly with mocked
    dependencies, without needing a real WebSocket, MQTT broker, or thread.

    open_ws_fn / setup_dashboard_fn / schedule_retry_fn default to the real
    _open_ha_websocket / _setup_menu_dashboard / threading.Timer-based
    scheduling, but can be overridden by tests.

    The WebSocket is opened INSIDE _setup_menu_dashboard, after the registry
    wait — NOT before it. The registry wait can take up to 60s; if the ws
    were opened here before calling _setup_menu_dashboard, the Supervisor
    would close it as idle before the Lovelace dashboard calls (which happen
    after the wait) could use it. This was the root cause of the
    'lovelace/dashboards/list call returned no result' error.

    A failure at any stage (cannot open WebSocket, exception during setup,
    or needs_retry returned True) schedules another attempt up to
    max_attempts, mirroring the original behavior — this function never lets
    an exception from setup_dashboard_fn propagate to its caller, since that
    was the original bug (an uncaught exception silently killed the regen
    thread and skipped the retry mechanism entirely).
    """
    open_ws_fn = open_ws_fn or _open_ha_websocket
    setup_dashboard_fn = setup_dashboard_fn or _setup_menu_dashboard

    def _default_schedule_retry() -> None:
        registry_watcher.refresh_registry()
        t = threading.Timer(
            retry_delay,
            _regen_menu_dashboard,
            kwargs={
                "registry_watcher": registry_watcher,
                "debug_mode": debug_mode,
                "attempt": attempt + 1,
                "max_attempts": max_attempts,
                "retry_delay": retry_delay,
                "open_ws_fn": open_ws_fn,
                "setup_dashboard_fn": setup_dashboard_fn,
                "schedule_retry_fn": schedule_retry_fn,
            },
        )
        t.daemon = True
        t.name = "nibe_menu_regen_retry"
        t.start()

    schedule_retry_fn = schedule_retry_fn or _default_schedule_retry

    log_startup.debug("Menu dashboard regen starting (attempt %d)...", attempt)
    try:
        needs_retry = setup_dashboard_fn(
            open_ws_fn,
            registry_watcher,
            debug_mode=debug_mode,
        )
    except Exception as e:  # noqa: BLE001 — best-effort I/O/network op; logged and degrades gracefully
        log_startup.warning(
            "Menu dashboard regen attempt %d failed unexpectedly: %s",
            attempt,
            e,
        )
        needs_retry = True

    if needs_retry and attempt < max_attempts:
        log_startup.debug(
            "Dashboard regen attempt %d: dynamic points not yet in registry — "
            "refreshing registry and retrying in %ss (attempt %d of %d)",
            attempt,
            retry_delay,
            attempt + 1,
            max_attempts,
        )
        schedule_retry_fn()
    elif needs_retry:
        log_startup.warning(
            "Dashboard regen: dynamic points still missing after %d attempts — giving up",
            max_attempts,
        )


def _on_enabled_state_change_factory(
    registry_watcher: "HAEntityRegistryWatcher",
    debug_mode: bool,
    lovelace_thread: threading.Thread | None = None,
) -> Callable:
    """Build the debounced on-enabled-state-change handler used by main().

    Extracted alongside _regen_menu_dashboard so the debounce wiring itself
    (cancel-and-reschedule) can be exercised in isolation from main()'s
    broader setup.

    If *lovelace_thread* is provided and still alive when the handler fires,
    the regen is skipped — the Lovelace setup thread is about to call
    _setup_menu_dashboard itself, so a second concurrent regen is redundant.
    This eliminates the double dashboard build on fresh starts where the
    initial menu auto-enable fires _on_enabled_state_change while the
    Lovelace setup thread is still running.

    publish_enabled_state() (which invokes this handler) is called from many
    places across the write executor, management executor, watcher thread,
    and poll thread — so this handler can genuinely be entered concurrently.
    _regen_timer_lock protects the cancel-and-reschedule sequence below;
    without it, two concurrent callers can each create their own Timer and
    overwrite _regen_timer[0], orphaning one of the two Timers (never
    cancelled) and defeating the debounce — the same failure mode
    _schedule_refresh_registry's lock in nibe_ha_integration.py exists to
    prevent.
    """
    _regen_timer: list[threading.Timer | None] = [None]  # mutable cell holding the pending Timer
    _regen_timer_lock = threading.Lock()

    def _on_enabled_state_change() -> None:
        if lovelace_thread is not None and lovelace_thread.is_alive():
            log_startup.debug("Menu dashboard regen skipped — Lovelace setup thread still running")
            return
        log_startup.debug("Menu dashboard regen scheduled (2s debounce)")

        def _fire(attempt: int = 1) -> None:
            with _regen_timer_lock:
                _regen_timer[0] = None
            _regen_menu_dashboard(registry_watcher, debug_mode, attempt=attempt)

        with _regen_timer_lock:
            if _regen_timer[0] is not None:
                _regen_timer[0].cancel()
            t = threading.Timer(2.0, _fire)
            t.daemon = True
            t.name = "nibe_menu_regen"
            _regen_timer[0] = t
            t.start()

    return _on_enabled_state_change


def _wire_menu_dashboard_regen(
    entity_manager: "EntityManager",
    registry_watcher: "HAEntityRegistryWatcher",
    debug_mode: bool,
    lovelace_thread: threading.Thread | None = None,
) -> None:
    """Wire the debounced regen handler into entity_manager. Thin glue
    between entity_manager's callback slot and the implementations in
    _on_enabled_state_change_factory / _regen_menu_dashboard above, both
    extracted for testability."""
    handler = _on_enabled_state_change_factory(
        registry_watcher,
        debug_mode,
        lovelace_thread=lovelace_thread,
    )
    entity_manager.set_on_enabled_state_change(handler)


# ============================================================================
# PUBLIC ENTRY POINTS
# ============================================================================


def copy_card_file() -> bool:
    """Copy the Lovelace card JS file to /homeassistant/www/. Called on startup."""
    return _copy_card_file()


def build_menu_points(yaml_path: str, controller_family: str | None = None) -> frozenset[int]:
    """Read menu_structure.yaml and return every point_id referenced anywhere
    in the menu hierarchy as a frozenset.

    This is the single source of truth for the "menus" mode's point set.
    Called once at startup by generate_nibe_mqtt.main() and stored into
    nibe_entity_detection.MODES['menus'] before apply_mode() runs —
    so the enabled set and the dashboard cards are always derived from the
    same source and can never silently diverge.

    controller_family : same meaning as in _build_menu_dashboard_config —
                     "air_water" or "water_water" (see
                     _detect_controller_family), or None to skip filtering
                     entirely. Passing the detected family here is what
                     keeps "menus" mode from enabling ground-source-only
                     points (GP1, brine circuit) on an air/water
                     installation and vice versa — without this, entity
                     enablement and the dashboard's own family filtering
                     would silently diverge, defeating the point of the
                     "single source of truth" comment above.

    Returns an empty frozenset if the file cannot be read or parsed,
    so a missing YAML degrades gracefully rather than crashing startup.
    """
    try:
        menu_structure = _load_menu_structure_yaml(yaml_path)
        if controller_family is not None:
            menu_structure = _filter_menu_structure_by_family(menu_structure, controller_family)
        points = _collect_menu_points(menu_structure)
        log_startup.debug("Built MENU_POINTS from YAML: %d unique point_ids", len(points))
        return frozenset(points)
    except (OSError, ValueError, yaml.YAMLError, AttributeError, TypeError) as e:
        # OSError/ValueError/yaml.YAMLError: see _load_menu_structure_yaml.
        # AttributeError/TypeError: a menu/setting/submenu entry isn't a
        # dict, so _collect_menu_points' .get() calls fail.
        log_startup.warning("Could not build MENU_POINTS from %s: %s", yaml_path, e)
        return frozenset()


def provision_lovelace_ui(
    version: str,
    device_name: str,
    registry_watcher: "HAEntityRegistryWatcher",
    debug_mode: bool = False,
    mode: str = "menus",
) -> None:
    """Open a WebSocket, register the card resource, and create/update dashboards.

    The Bridge dashboard and card resource are provisioned in every mode.
    The Nibe Menus dashboard is only built when mode == "menus" — see
    _setup_lovelace. Default mode="menus" preserves prior behaviour for
    any caller that doesn't pass it explicitly.

    Safe to call on every startup — all steps are idempotent.
    """
    _setup_lovelace(version, device_name, registry_watcher, debug_mode, mode=mode)


def schedule_menu_dashboard_regen(
    entity_manager: "EntityManager",
    registry_watcher: "HAEntityRegistryWatcher",
    debug_mode: bool,
    lovelace_thread: threading.Thread | None = None,
) -> None:
    """Wire the debounced menu dashboard regeneration callback into entity_manager."""
    _wire_menu_dashboard_regen(
        entity_manager, registry_watcher, debug_mode, lovelace_thread=lovelace_thread
    )


def teardown_lovelace(remove_frontend: bool) -> None:
    """Remove dashboard, resource registration, and card file on clean uninstall."""
    _teardown_lovelace(remove_frontend)


def remove_menu_dashboard() -> None:
    """Remove the Nibe Menus dashboard if present. Idempotent; safe to call
    on every non-menus-mode startup — see _remove_menu_dashboard()."""
    _remove_menu_dashboard()
