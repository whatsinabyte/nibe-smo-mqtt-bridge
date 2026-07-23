"""
nibe_lovelace.py
================
Lovelace UI provisioning for the Nibe S-Series MQTT Bridge.

Handles all interaction with the Home Assistant frontend:
  - Copying the Lovelace card JS file to /config/www/
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
import yaml

from nibe_entity_detection import clean_string, clean_unit

log_startup = logging.getLogger("nibe.startup")

def _copy_card_file() -> bool:
    """Copy the Lovelace card JS file to the HA www directory."""
    src     = "/app/nibe-entity-manager-card.js"
    dst_dir = "/config/www"
    dst     = os.path.join(dst_dir, "nibe-entity-manager-card.js")
    try:
        os.makedirs(dst_dir, exist_ok=True)
        shutil.copy2(src, dst)
        log_startup.info("Card file copied to %s", dst)
        return True
    except Exception as e:
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

_DASHBOARD_SLUG  = "nibe-bridge"
_DASHBOARD_TITLE = "Nibe Bridge"
_DASHBOARD_ICON  = "mdi:heat-pump"
_CARD_TYPE       = "custom:nibe-entity-manager-card"


_MENU_DASHBOARD_SLUG  = "nibe-menus"
_MENU_DASHBOARD_TITLE = "Nibe Menus"
_MENU_DASHBOARD_FLAG  = "/data/lovelace_menus_provisioned"
_LOVELACE_FLAG        = "/data/lovelace_provisioned"


def _build_point_defaults(all_points_by_id: dict[int, dict]) -> dict[int, str]:
    """Build a point_id → formatted-default string map for menu annotations.

    Only includes points where the default is meaningful:
    - Writable MODBUS_HOLDING_REGISTER with a non-degenerate range
    - intDefaultValue != 0 or minValue != 0  (suppress ambiguous zeros)

    The returned string is already formatted with divisor applied and unit
    appended, ready to embed in a section-divider label.
    """
    defaults: dict[int, str] = {}
    for point_id, point in all_points_by_id.items():
        meta = point.get('metadata', {})
        if not meta.get('isWritable'):
            continue
        if meta.get('modbusRegisterType') != 'MODBUS_HOLDING_REGISTER':
            continue
        min_val = meta.get('minValue', 0)
        max_val = meta.get('maxValue', 0)
        if min_val == max_val:
            continue
        int_default = meta.get('intDefaultValue')
        if int_default is None:
            continue
        if int_default == 0 and min_val == 0 and max_val > 1:
            continue
        divisor = meta.get('divisor', 1) or 1
        display = f"{int_default / divisor:g}"
        unit    = clean_unit(meta.get('unit') or meta.get('shortUnit'))
        defaults[point_id] = f"{display} {unit}".strip() if unit else display
    return defaults


def _build_dynamic_injection(
    dynamic_point_map,
    active_dynamic_points: set[int],
    registry_watcher,
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
                title = point.get('display_title') or point.get('title') or f'Point {dyn_pid}'
                meta  = point.get('metadata', {})
                div   = meta.get('divisor', 1) or 1
                mn    = meta.get('minValue', 0) / div
                mx    = meta.get('maxValue', 0) / div
                unit  = clean_unit(meta.get('unit'))
                rng   = f'{mn:g} – {mx:g}{" " + unit if unit else ""}'
                dflt  = (point_defaults or {}).get(dyn_pid, '')
                items.append((eid, title, rng, dflt))
        if items:
            injection[entry.point_id] = items
    return injection


def _build_menu_view(
    menu:           dict,
    registry_watcher,
    known_dynamic:  set[int] | None = None,
    absent_dynamic: set[int] | None = None,
    point_defaults: dict[int, str] | None = None,
    dynamic_injection: dict[int, list[tuple[str, str, str, str]]] | None = None,
) -> list:
    """Build a list of Lovelace cards for a single top-level menu.

    Parameters
    ----------
    menu :           Top-level menu dict from menu_structure.yaml.
    registry_watcher: HAEntityRegistryWatcher — resolves point_id → entity_id.
    known_dynamic :  Points seen at least once in a bulk fetch on this
                     installation.  A point absent from this set has never
                     appeared — hardware not installed or feature inactive.
    absent_dynamic : Subset of known_dynamic currently absent from the API
                     (accessory unplugged / setting deactivated).
    point_defaults : point_id → formatted default string, from
                     _build_point_defaults().  Appended to section-divider
                     labels as "· default: X" where present.
    dynamic_injection : controlling_point_id → list of entity_ids for active
                     dynamic points controlled by that point.  These are
                     injected below the controlling entity row in the card,
                     labelled with a ↳ indent to show the relationship.
    """
    known_dynamic      = known_dynamic      or set()
    absent_dynamic     = absent_dynamic     or set()
    point_defaults     = point_defaults     or {}
    dynamic_injection  = dynamic_injection  or {}
    cards = []

    def _alert(alert_type: str, title: str, text: str) -> str:
        """Render an HA native alert box (HA 2022.9+).

        alert_type: "warning", "info", "error", "success"
        Renders as a coloured box with left accent and icon in HA.
        Falls back gracefully to plain text in older HA versions.
        """
        return f'<ha-alert alert-type="{alert_type}" title="{title}">{text.strip()}</ha-alert>'

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
            md_lines.append(m["description"].strip())
            md_lines.append("")

        # Menu-level callouts using ha-alert for native coloured boxes
        if m.get("warning"):
            md_lines.append(_alert("warning", "Warning", m["warning"]))
            md_lines.append("")
        if m.get("note"):
            md_lines.append(_alert("info", "Note", m["note"]))
            md_lines.append("")
        if m.get("tip"):
            md_lines.append(_alert("success", "Tip", m["tip"]))
            md_lines.append("")

        # Items not available via local API
        if m.get("local_api") is False:
            md_lines.append(_alert("info", "Not available via local API", "This feature is configured on the controller display and has no local API register."))
            md_lines.append("")
            if md_lines:
                cards.append({"type": "markdown", "content": "\n".join(md_lines)})
            return

        # Collect per-setting callouts BEFORE appending the markdown card
        # so they are included in the same card as the section description.
        for s in m.get("settings", []):
            label     = s.get("label", "")
            s_warning = s.get("warning", "")
            s_note    = s.get("note", "")
            s_tip     = s.get("tip", "")
            if s_warning:
                md_lines.append(_alert("warning", label, s_warning))
                md_lines.append("")
            elif s_note:
                md_lines.append(_alert("info", label, s_note))
                md_lines.append("")
            elif s_tip:
                md_lines.append(_alert("success", label, s_tip))
                md_lines.append("")

        # Render section heading + description + callouts as a markdown card
        cards.append({"type": "markdown", "content": "\n".join(md_lines)})

        # Build a single entities card for this section using HA's native
        # section-divider rows (HA 2024.1+) to label each control.
        entities_rows = []

        for s in m.get("settings", []):
            point_id  = s.get("point_id")
            label     = s.get("label", "")
            rng       = s.get("range", "")

            # Dynamic points: skip entirely regardless of active state.
            # When active they appear via injection below the controlling point.
            # When inactive they should not appear at all.
            if point_id and point_id in known_dynamic:
                continue

            # Section divider: label · range · default (where known)
            section_label = label
            if rng:
                section_label += f"  ·  {rng}"
            if point_id and point_id in point_defaults:
                section_label += f"  ·  default: {point_defaults[point_id]}"
            entities_rows.append({
                "type":  "section",
                "label": section_label,
            })

            if point_id:
                entity_id = registry_watcher.entity_id_for(point_id)
                if entity_id:
                    entities_rows.append({"entity": entity_id})
                    for dyn_entity_id, dyn_title, dyn_rng, dyn_dflt in dynamic_injection.get(point_id, []):
                        divider = f"↳ {dyn_title}  ·  {dyn_rng}"
                        if dyn_dflt:
                            divider += f"  ·  default: {dyn_dflt}"
                        entities_rows.append({"type": "section", "label": divider})
                        entities_rows.append({"entity": dyn_entity_id})
                elif point_id not in known_dynamic:
                    entities_rows.append({
                        "type":  "section",
                        "label": "↳ not enabled",
                    })

        if entities_rows:
            cards.append({
                "type":     "entities",
                "entities": entities_rows,  # type: ignore[dict-item]
            })

        # Recurse into submenus
        for sub in m.get("submenus", []):
            _render_section(sub, depth + 1)

    _render_section(menu, depth=2)

    # Footer
    cards.append({
        "type":    "markdown",
        "content": "---\n*Source: NIBE SMO S40 installer manual — Chapter 9, Control – Menus*",
    })

    return cards


def _build_unplaced_view(
    bulk_data:         dict[int, dict],
    menu_yaml_points:  set[int],
    registry_watcher,
    point_defaults:    dict[int, str],
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
    # Patterns that indicate a multi-system series — grouped separately
    _GROUP_PATTERNS = [
        r'climate system [2-8]',
        r'zone \d+',
        r'ECS\d+',
        r'FLM [2-4]',
        r'EB1[0-9][2-9]',
        r'ERS [5-8]',
        r'RMU',
        r'smart energy source',
        r'tariff',
        r'return time fan',
        r'filter replacement',
        r'desired room temperature for zone',
    ]

    unplaced_writable  = []
    unplaced_grouped   = []
    unplaced_readonly  = []

    for point_id, point_data in sorted(bulk_data.items()):
        if point_id in menu_yaml_points:
            continue
        meta = point_data.get('metadata', {})
        reg  = meta.get('modbusRegisterType', '')
        if reg not in ('MODBUS_HOLDING_REGISTER', 'MODBUS_INPUT_REGISTER'):
            continue
        mn   = meta.get('minValue', 0)
        mx   = meta.get('maxValue', 0)
        if mn == mx:
            continue  # degenerate range
        title = point_data.get('display_title') or point_data.get('title', f'Point {point_id}')
        title = clean_string(title) or f'Point {point_id}'
        unit  = clean_unit(meta.get('unit'))
        div   = meta.get('divisor', 1) or 1
        rng   = f"{mn/div:g} – {mx/div:g}{' ' + unit if unit else ''}"
        entry = (point_id, title, rng)

        if meta.get('isWritable') and reg == 'MODBUS_HOLDING_REGISTER':
            # Check if it's part of a repetitive series
            is_grouped = any(re.search(pat, title, re.I) for pat in _GROUP_PATTERNS)
            if is_grouped:
                unplaced_grouped.append(entry)
            else:
                unplaced_writable.append(entry)
        elif reg in ('MODBUS_HOLDING_REGISTER', 'MODBUS_INPUT_REGISTER'):
            unplaced_readonly.append(entry)

    if not unplaced_writable and not unplaced_grouped and not unplaced_readonly:
        return None

    cards = []

    cards.append({"type": "markdown", "content": (
        "<h2><font color='#9C1924'>Unplaced settings (debug)</font></h2>\n\n"
        "Firmware points present in the bulk fetch but not yet documented "
        "in `menu_structure.yaml`. This tab is only visible in debug mode.\n\n"
        f"**{len(unplaced_writable)} writable (review)** · "
        f"**{len(unplaced_grouped)} writable (series/grouped)** · "
        f"**{len(unplaced_readonly)} read-only**"
    )})

    def _section_rows(entries: list[tuple], label: str) -> list:
        rows = [{"type": "section", "label": label}]
        for point_id, title, rng in entries:
            default_str = point_defaults.get(point_id, '')
            divider = f"{title}  ·  {rng}"
            if default_str:
                divider += f"  ·  default: {default_str}"
            rows.append({"type": "section", "label": divider})
            entity_id = registry_watcher.entity_id_for(point_id)
            if entity_id:
                rows.append({"entity": entity_id})
            else:
                rows.append({"type": "section",
                             "label": f"↳ not enabled (point {point_id})"})
        return rows

    if unplaced_writable:
        rows = _section_rows(
            unplaced_writable,
            f"Writable — review for YAML ({len(unplaced_writable)} points)"
        )
        cards.append({"type": "entities", "entities": rows})  # type: ignore[dict-item]

    if unplaced_grouped:
        rows = _section_rows(
            unplaced_grouped,
            f"Writable — multi-system series ({len(unplaced_grouped)} points)"
        )
        cards.append({"type": "entities", "entities": rows})  # type: ignore[dict-item]

    if unplaced_readonly:
        rows = _section_rows(
            unplaced_readonly,
            f"Read-only ({len(unplaced_readonly)} points)"
        )
        cards.append({"type": "entities", "entities": rows})  # type: ignore[dict-item]

    return {
        "title": "⚙ Unplaced (debug)",
        "path":  "menu-unplaced-debug",
        "cards": [{"type": "vertical-stack", "cards": cards}],
    }


def _build_menu_dashboard_config(
    menu_structure:    list,
    registry_watcher,
    known_dynamic:     set[int] | None = None,
    absent_dynamic:    set[int] | None = None,
    point_defaults:    dict[int, str] | None = None,
    dynamic_injection: dict[int, list[tuple[str, str, str, str]]] | None = None,
    debug_mode:        bool = False,
    bulk_data:         dict[int, dict] | None = None,
    menu_yaml_points:  set[int] | None = None,
) -> dict:
    """Build the full Lovelace dashboard config for the menu views.

    Each top-level menu becomes a separate view (tab) containing a
    vertical-stack card with interleaved markdown and entities cards.
    """
    views = []

    for menu in menu_structure:
        cards = _build_menu_view(
            menu, registry_watcher,
            known_dynamic  or set(),
            absent_dynamic or set(),
            point_defaults or {},
            dynamic_injection or {},
        )
        if not cards:
            continue

        # No icon — HA shows either icon OR title in the tab bar, not both
        # (without the user enabling a per-dashboard UI toggle). Title is
        # more useful on mobile so we omit the icon entirely.
        views.append({
            "title": f"{menu['id']} {menu['title']}",
            "path":  f"menu-{menu['id'].replace('.', '-')}",
            "cards": [{
                "type":  "vertical-stack",
                "cards": cards,
            }],
        })

    if not views:
        return None

    # Debug-only: append unplaced settings view
    if debug_mode and bulk_data and menu_yaml_points is not None:
        unplaced_view = _build_unplaced_view(
            bulk_data, menu_yaml_points,
            registry_watcher, point_defaults or {},
        )
        if unplaced_view:
            views.append(unplaced_view)

    return {"views": views}


def _collect_menu_points(menus: list) -> set[int]:
    """Walk the full menu hierarchy and collect every point_id referenced
    in any setting, at any nesting depth. Module-level (not a nested
    closure) so this pure recursive logic can be unit tested directly
    against hand-built menu structures, independent of the real
    menu_structure.yaml or any WebSocket/registry dependency."""
    pids: set[int] = set()
    for m in menus:
        for s in m.get('settings', []):
            pid = s.get('point_id')
            if pid:
                pids.add(pid)
        pids.update(_collect_menu_points(m.get('submenus', [])))
    return pids


def _build_point_to_menu(menus: list, result: dict | None = None) -> dict:
    """Build the reverse lookup point_id -> (menu_id, menu_title) by
    walking the full menu hierarchy. Module-level for the same testability
    reason as _collect_menu_points above."""
    if result is None:
        result = {}
    for m in menus:
        mid   = m.get('id', '')
        title = m.get('title', '')
        for s in m.get('settings', []):
            pid = s.get('point_id')
            if pid:
                result[pid] = (mid, title)
        _build_point_to_menu(m.get('submenus', []), result)
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
        (d for d in dashboards_response.get("result", [])
         if d.get("url_path") == slug),
        None,
    )
    return existing is None


def _setup_menu_dashboard(open_ws_fn, registry_watcher, debug_mode: bool = False) -> bool:
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
        with open(menu_path, "r", encoding="utf-8") as f:
            menu_data = yaml.safe_load(f)
        menu_structure = menu_data.get("menus", [])
    except Exception as e:
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
        pid for pid in all_menu_points
        if pid in entity_manager.all_points_by_id
    }

    # Wait for the registry watcher to resolve entity IDs for both:
    # 1. All available menu points (needed on every startup — without this
    #    the _unique_id_map is empty and all entities show as "not enabled")
    # 2. All active dynamic points (needed for injection after a controlling
    #    point is flipped — these arrive via registry create events)
    # We poll until BOTH sets are stable. Dynamic points are checked
    # separately with a shorter inner timeout so we don't block indefinitely
    # if an accessory is disconnected.
    _step        = 0.5
    _limit       = 60.0
    _waited      = 0.0
    _prev_count  = -1
    _stable_for  = 0.0
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

    active_dynamic = entity_manager.active_dynamic_points

    while _waited < _limit:
        time.sleep(_step)
        _waited += _step

        menu_resolved = sum(
            1 for p in available_menu_points
            if registry_watcher.entity_id_for(p)
        )
        dyn_resolved = sum(
            1 for p in active_dynamic
            if registry_watcher.entity_id_for(p)
        )
        current_count = menu_resolved + dyn_resolved

        if current_count == _prev_count:
            _stable_for += _step
            # Don't accept stability if we're well below the expected count —
            # on a fresh start HA creates entities in waves and the count may
            # pause between waves, producing a false stable window.
            menu_complete = (
                menu_resolved >= len(available_menu_points) * _completeness_threshold
            )
            if _stable_for >= _stable_need and menu_resolved > 0 and menu_complete:
                # All dynamic points resolved — ideal exit
                if dyn_resolved == len(active_dynamic):
                    log_startup.debug(
                        "Registry stable: %d/%d menu + %d/%d dynamic after %.1fs",
                        menu_resolved, len(available_menu_points),
                        dyn_resolved, len(active_dynamic), _waited,
                    )
                    break
                # Menu stable but dynamic still missing — wait a bit more
                # but don't hold up the retry mechanism indefinitely
                if _stable_for >= 8.0:
                    log_startup.debug(
                        "Registry stable at %d/%d menu, %d/%d dynamic after %.1fs — proceeding",
                        menu_resolved, len(available_menu_points),
                        dyn_resolved, len(active_dynamic), _waited,
                    )
                    break
        else:
            _stable_for = 0.0
            _prev_count = current_count
    else:
        log_startup.warning(
            "Registry wait timed out — %d/%d menu + %d/%d dynamic resolved",
            sum(1 for p in available_menu_points if registry_watcher.entity_id_for(p)),
            len(available_menu_points),
            sum(1 for p in active_dynamic if registry_watcher.entity_id_for(p)),
            len(active_dynamic),
        )

    # Build dashboard config
    known_dynamic      = entity_manager.dynamic_point_map.all_known_dynamic_point_ids()
    absent_dynamic     = known_dynamic - entity_manager.active_dynamic_points
    point_defaults     = _build_point_defaults(entity_manager.all_points_by_id)
    dynamic_injection  = _build_dynamic_injection(
        entity_manager.dynamic_point_map,
        entity_manager.active_dynamic_points,
        registry_watcher,
        entity_manager.all_points_by_id,
        point_defaults,
    )
    dashboard_config = _build_menu_dashboard_config(
        menu_structure, registry_watcher, known_dynamic, absent_dynamic,
        point_defaults, dynamic_injection,
        debug_mode       = debug_mode,
        bulk_data        = entity_manager.bulk_data,
        menu_yaml_points = all_menu_points,
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
        log_startup.warning(
            "Menu dashboard: could not open WebSocket for Lovelace API calls"
        )
        return True   # signal retry
    ws, next_id = ws_result

    try:
        return _setup_menu_dashboard_lovelace(
            ws, next_id, dashboard_config, entity_manager,
            registry_watcher, available_menu_points, active_dynamic,
        )
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _setup_menu_dashboard_lovelace(
    ws, next_id, dashboard_config: dict,
    entity_manager, registry_watcher, available_menu_points, active_dynamic,
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
        resp = _ws_call(ws, next_id(), {
            "type":       "lovelace/dashboards/create",
            "url_path":   _MENU_DASHBOARD_SLUG,
            "mode":       "storage",
            "title":      _MENU_DASHBOARD_TITLE,
            "icon":       "mdi:book-open-outline",
            "show_in_sidebar": True,
        })
        if not resp.get("success"):
            error_msg = str(resp.get("error", {}))
            if "url_already_exists" not in error_msg and "already in use" not in error_msg:
                log_startup.warning("Could not create Nibe Menus dashboard: %s", resp)
                return False
            log_startup.debug("Nibe Menus dashboard already exists — proceeding to update config")
    elif not dashboards.get("success"):
        log_startup.warning(
            "lovelace/dashboards/list call returned no result — "
            "will retry."
        )
        return True
    else:
        log_startup.debug("Nibe Menus dashboard already exists — skipping create")

    # Save the dashboard config (views + cards)
    resp = _ws_call(ws, next_id(), {
        "type":     "lovelace/config/save",
        "url_path": _MENU_DASHBOARD_SLUG,
        "config":   dashboard_config,
    })

    log_startup.debug("lovelace/config/save response: %s", resp)
    if resp.get("success"):
        view_count = len(dashboard_config["views"])
        log_startup.info(
            "Nibe Menus dashboard provisioned with %d menu view(s). "
            "Find it in your HA sidebar under '%s'.",
            view_count, _MENU_DASHBOARD_TITLE,
        )
        # Fire lovelace_updated event so connected browsers reload.
        _ws_call(ws, next_id(), {
            "type":       "fire_event",
            "event_type": "lovelace_updated",
            "event_data": {"url_path": _MENU_DASHBOARD_SLUG},
        })

        # Verify all active dynamic points are in the registry.
        # Only retry if menu entities resolved correctly (registry is up)
        # but dynamic point(s) still missing.
        menu_resolved = sum(1 for p in available_menu_points if registry_watcher.entity_id_for(p))
        missing_dynamic = [
            p for p in entity_manager.active_dynamic_points
            if not registry_watcher.entity_id_for(p)
        ]
        if missing_dynamic and menu_resolved > 0:
            log_startup.debug(
                "Dashboard saved but %d dynamic point(s) not yet in registry — "
                "retry needed: %s", len(missing_dynamic), missing_dynamic,
            )
            return True   # needs retry
        return False      # all good
    else:
        log_startup.warning("Menu dashboard config save failed: %s", resp)
        return False


def _open_ha_websocket():
    """Open and authenticate a WebSocket connection to the HA Supervisor.

    Returns (ws, next_id_callable) on success, or None if the connection
    cannot be established (no token, import error, auth failure).
    """
    supervisor_token = os.environ.get('SUPERVISOR_TOKEN')
    if not supervisor_token:
        return None

    try:
        import websocket
        ws = websocket.create_connection("ws://supervisor/core/websocket", timeout=10)
    except ImportError:
        log_startup.warning("websocket-client not installed — WebSocket unavailable")
        return None
    except Exception as e:
        log_startup.warning("Could not connect to HA WebSocket: %s", e)
        return None

    _mid = 0
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
    except Exception as e:
        log_startup.warning("HA WebSocket auth error: %s", e)
        try:
            ws.close()
        except Exception:
            pass
        return None


def _setup_lovelace(version: str, device_name: str, registry_watcher=None,
                     debug_mode: bool = False, mode: str = "menus") -> None:
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

    supervisor_token = os.environ.get('SUPERVISOR_TOKEN')
    if not supervisor_token:
        log_startup.debug(
            "No SUPERVISOR_TOKEN — skipping Lovelace setup "
            "(running outside HA add-on environment)"
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

    except Exception as e:
        log_startup.warning("Lovelace setup failed: %s", e)
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _setup_lovelace_resource(ws, next_id, versioned_url: str) -> None:
    """Register or update the card JS file as a Lovelace module resource.

    Called from _setup_lovelace with an already-authenticated WebSocket.
    """
    resp      = _ws_call(ws, next_id(), {"type": "lovelace/resources/list"})
    resources = resp.get("result", [])

    # Find all existing registrations for this card (duplicates cause
    # "already defined" errors in Safari when two versions are loaded).
    matching = [r for r in resources if "nibe-entity-manager-card.js" in r.get("url", "")]

    # Delete any duplicates beyond the first
    for dup in matching[1:]:
        _ws_call(ws, next_id(), {
            "type":        "lovelace/resources/delete",
            "resource_id": dup.get("id"),
        })
        log_startup.info("Removed duplicate Lovelace resource: %s", dup.get("url"))

    existing = matching[0] if matching else None

    if existing is not None:
        if existing.get("url") == versioned_url:
            log_startup.debug("Lovelace resource already current (%s) — no update needed", versioned_url)
            return
        resp = _ws_call(ws, next_id(), {
            "type":        "lovelace/resources/update",
            "resource_id": existing.get("id"),
            "res_type":    "module",
            "url":         versioned_url,
        })
        action = "Updated"
    else:
        resp = _ws_call(ws, next_id(), {
            "type":     "lovelace/resources/create",
            "res_type": "module",
            "url":      versioned_url,
        })
        action = "Registered"

    if resp.get("success"):
        log_startup.info("%s Lovelace resource: %s", action, versioned_url)
    else:
        log_startup.warning("Lovelace resource %s failed: %s", action.lower(), resp)


def _setup_lovelace_dashboard(ws, next_id, device_name: str, flag_file: str) -> None:
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

    # Check if dashboard already exists before attempting create — avoids HA
    # logging a system-log error for "URL already in use" on every restart
    # after a container rebuild that wiped the flag file.
    dashboards = _ws_call(ws, next_id(), {"type": "lovelace/dashboards/list"})
    existing   = next(
        (d for d in dashboards.get("result", [])
         if d.get("url_path") == _DASHBOARD_SLUG),
        None,
    )
    if existing is not None:
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

    resp = _ws_call(ws, next_id(), {
        "type":       "lovelace/dashboards/create",
        "url_path":   _DASHBOARD_SLUG,
        "mode":       "storage",
        "title":      _DASHBOARD_TITLE,
        "icon":       _DASHBOARD_ICON,
        "show_in_sidebar": True,
        "require_admin":   False,
    })

    if not resp.get("success"):
        error      = resp.get("error", {})
        error_code = error.get("translation_key") or error.get("code", "")
        error_msg  = error.get("message", "")
        if error_code == "url_already_exists" or "already in use" in error_msg.lower():
            log_startup.info(
                "Nibe Bridge dashboard already exists (/%s) — writing flag to skip future attempts",
                _DASHBOARD_SLUG,
            )
            try:
                with open(flag_file, "w") as f:
                    f.write("provisioned\n")
            except OSError as e:
                log_startup.warning("Could not write lovelace provisioned flag: %s", e)
        else:
            log_startup.warning("Could not create Nibe Bridge dashboard: %s", resp)
        return

    dashboard_id = resp.get("result", {}).get("id")
    log_startup.info(
        "Created Nibe Bridge dashboard (id=%s, url_path=/%s)",
        dashboard_id, _DASHBOARD_SLUG,
    )

    view_title = device_name
    dashboard_config = {
        "views": [{
            "title": view_title,
            "path":  "home",
            "icon":  _DASHBOARD_ICON,
            "type":  "panel",
            "cards": [{
                "type":                  _CARD_TYPE,
                "title":                 "",
                "pageSize":              50,
                "suppressInitialToasts": True,
            }],
        }]
    }

    resp = _ws_call(ws, next_id(), {
        "type":     "lovelace/config/save",
        "url_path": _DASHBOARD_SLUG,
        "config":   dashboard_config,
    })

    if resp.get("success"):
        log_startup.info(
            "Nibe Bridge dashboard configured with '%s' card. "
            "Find it in your HA sidebar under '%s'.",
            _CARD_TYPE, _DASHBOARD_TITLE,
        )
        try:
            with open(flag_file, "w") as f:
                f.write("provisioned\n")
        except OSError as e:
            log_startup.warning("Could not write lovelace provisioned flag: %s", e)
    else:
        log_startup.warning(
            "Dashboard created but card config could not be written: %s. "
            "You can add the '%s' card manually.",
            resp, _CARD_TYPE,
        )


def _ws_call(ws, msg_id: int, payload: dict, timeout: int = 10) -> dict:
    """Send a single WebSocket message and return the parsed response.

    Attaches the message ID, sends, and reads messages until it finds the
    result matching msg_id.  Intermediate event messages (e.g. from active
    subscriptions) are discarded so they do not corrupt command/response pairs.
    Returns an empty dict on timeout or error, including if the connection
    has already failed (e.g. BrokenPipeError on send) — callers should treat
    an empty dict as "this call did not succeed" regardless of cause.
    """
    try:
        ws.send(json.dumps({**payload, "id": msg_id}))
    except Exception as e:
        log_startup.debug("_ws_call: send failed (id=%s): %s", msg_id, e)
        return {}
    deadline = time.time() + timeout
    ws.settimeout(timeout)
    try:
        while time.time() < deadline:
            raw = ws.recv()
            if not raw:
                break
            msg = json.loads(raw)
            if msg.get("id") == msg_id and msg.get("type") == "result":
                return msg
    except Exception:
        pass
    finally:
        ws.settimeout(None)
    return {}


def _teardown_lovelace() -> None:
    """Remove the Nibe Bridge dashboard, its Lovelace resource registration,
    and the card file from /config/www/ on clean shutdown when the
    remove_frontend option is set to true (surfaced as NIBE_REMOVE_FRONTEND=1
    by run.sh).

    This is intentionally opt-in rather than running on every restart:
    - Normal restarts and add-on updates must NOT touch the dashboard.
    - Only a deliberate uninstall / data-removal flow should clean up.

    All steps are individually guarded — partial failures are logged as
    warnings so a broken WebSocket connection does not prevent the card
    file from being removed (or vice versa).
    """
    if os.environ.get('NIBE_REMOVE_FRONTEND') != '1':
        return

    log_startup.info("NIBE_REMOVE_FRONTEND=1 — removing Lovelace dashboard and resources")

    supervisor_token = os.environ.get('SUPERVISOR_TOKEN')

    # ── Remove card file from /config/www/ ───────────────────────────────────
    card_dst = "/config/www/nibe-entity-manager-card.js"
    try:
        if os.path.exists(card_dst):
            os.remove(card_dst)
            log_startup.info("Removed card file: %s", card_dst)
        else:
            log_startup.debug("Card file not found at %s — already removed", card_dst)
    except Exception as e:
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
            resp       = _ws_call(ws, _next_id(), {"type": "lovelace/dashboards/list"})
            dashboards = resp.get("result", [])
            existing   = next(
                (d for d in dashboards if d.get("url_path") == _DASHBOARD_SLUG),
                None,
            )
            if existing is not None:
                resp = _ws_call(ws, _next_id(), {
                    "type":         "lovelace/dashboards/delete",
                    "dashboard_id": existing.get("id"),
                })
                if resp.get("success"):
                    log_startup.info("Removed Nibe Bridge dashboard (id=%s)", existing.get("id"))
                else:
                    log_startup.warning("Could not remove dashboard: %s", resp)
            else:
                log_startup.debug("Nibe Bridge dashboard not found — already removed")
        except Exception as e:
            log_startup.warning("Dashboard removal failed: %s", e)

        # ── Remove Lovelace resource registration ─────────────────────────────
        try:
            resp      = _ws_call(ws, _next_id(), {"type": "lovelace/resources/list"})
            resources = resp.get("result", [])
            existing  = next(
                (r for r in resources if "nibe-entity-manager-card.js" in r.get("url", "")),
                None,
            )
            if existing is not None:
                resp = _ws_call(ws, _next_id(), {
                    "type":        "lovelace/resources/delete",
                    "resource_id": existing.get("id"),
                })
                if resp.get("success"):
                    log_startup.info("Removed Lovelace resource registration")
                else:
                    log_startup.warning("Could not remove Lovelace resource: %s", resp)
            else:
                log_startup.debug("Lovelace resource not found — already removed")
        except Exception as e:
            log_startup.warning("Resource removal failed: %s", e)

    finally:
        try:
            ws.close()
        except Exception:
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
    supervisor_token = os.environ.get('SUPERVISOR_TOKEN')
    if not supervisor_token:
        log_startup.debug(
            "No SUPERVISOR_TOKEN — skipping menu dashboard teardown "
            "(running outside HA add-on environment)"
        )
        return

    result = _open_ha_websocket()
    if result is None:
        log_startup.debug(
            "Could not open HA WebSocket for menu dashboard teardown — "
            "will retry on next startup"
        )
        return
    ws, _next_id = result

    try:
        resp       = _ws_call(ws, _next_id(), {"type": "lovelace/dashboards/list"})
        dashboards = resp.get("result", [])
        list_succeeded = resp.get("success", False)
        existing   = next(
            (d for d in dashboards if d.get("url_path") == _MENU_DASHBOARD_SLUG),
            None,
        )
        if existing is not None:
            resp = _ws_call(ws, _next_id(), {
                "type":         "lovelace/dashboards/delete",
                "dashboard_id": existing.get("id"),
            })
            if resp.get("success"):
                log_startup.info("Removed Nibe Menus dashboard (id=%s)", existing.get("id"))
            else:
                log_startup.warning("Could not remove Nibe Menus dashboard: %s", resp)
                list_succeeded = False  # suppress flag removal — retry next startup
        elif not list_succeeded:
            log_startup.debug("Nibe Menus dashboard list call returned no result — "
                              "will retry on next startup")
        else:
            log_startup.debug("Nibe Menus dashboard not found — nothing to remove")
    except Exception as e:
        log_startup.warning("Menu dashboard teardown failed: %s", e)
        list_succeeded = False
    finally:
        try:
            ws.close()
        except Exception:
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
    registry_watcher,
    debug_mode: bool,
    attempt: int = 1,
    max_attempts: int = 3,
    retry_delay: float = 3.0,
    open_ws_fn=None,
    setup_dashboard_fn=None,
    schedule_retry_fn=None,
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

    def _default_schedule_retry():
        registry_watcher.refresh_registry()
        t = threading.Timer(
            retry_delay, _regen_menu_dashboard,
            kwargs={
                'registry_watcher': registry_watcher,
                'debug_mode': debug_mode,
                'attempt': attempt + 1,
                'max_attempts': max_attempts,
                'retry_delay': retry_delay,
                'open_ws_fn': open_ws_fn,
                'setup_dashboard_fn': setup_dashboard_fn,
                'schedule_retry_fn': schedule_retry_fn,
            },
        )
        t.daemon = True
        t.name = "nibe_menu_regen_retry"
        t.start()

    schedule_retry_fn = schedule_retry_fn or _default_schedule_retry

    log_startup.debug("Menu dashboard regen starting (attempt %d)...", attempt)
    try:
        needs_retry = setup_dashboard_fn(
            open_ws_fn, registry_watcher, debug_mode=debug_mode,
        )
    except Exception as e:
        log_startup.warning(
            "Menu dashboard regen attempt %d failed unexpectedly: %s",
            attempt, e,
        )
        needs_retry = True

    if needs_retry and attempt < max_attempts:
        log_startup.debug(
            "Dashboard regen attempt %d: dynamic points not yet in registry — "
            "refreshing registry and retrying in %ss (attempt %d of %d)",
            attempt, retry_delay, attempt + 1, max_attempts,
        )
        schedule_retry_fn()
    elif needs_retry:
        log_startup.warning(
            "Dashboard regen: dynamic points still missing after %d attempts — giving up",
            max_attempts,
        )


def _on_enabled_state_change_factory(
    registry_watcher,
    debug_mode: bool,
    lovelace_thread: threading.Thread | None = None,
):
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
    """
    _regen_timer = [None]  # mutable cell holding the pending Timer

    def _on_enabled_state_change():
        if lovelace_thread is not None and lovelace_thread.is_alive():
            log_startup.debug(
                "Menu dashboard regen skipped — Lovelace setup thread still running"
            )
            return
        log_startup.debug("Menu dashboard regen scheduled (2s debounce)")
        if _regen_timer[0] is not None:
            _regen_timer[0].cancel()

        def _fire(attempt: int = 1):
            _regen_timer[0] = None
            _regen_menu_dashboard(registry_watcher, debug_mode, attempt=attempt)

        t = threading.Timer(2.0, _fire)
        t.daemon = True
        t.name = "nibe_menu_regen"
        _regen_timer[0] = t
        t.start()

    return _on_enabled_state_change


def _wire_menu_dashboard_regen(
    entity_manager,
    registry_watcher,
    debug_mode: bool,
    lovelace_thread: threading.Thread | None = None,
) -> None:
    """Wire the debounced regen handler into entity_manager. Thin glue
    between entity_manager's callback slot and the implementations in
    _on_enabled_state_change_factory / _regen_menu_dashboard above, both
    extracted for testability."""
    handler = _on_enabled_state_change_factory(
        registry_watcher, debug_mode, lovelace_thread=lovelace_thread,
    )
    entity_manager.set_on_enabled_state_change(handler)


# ============================================================================
# PUBLIC ENTRY POINTS
# ============================================================================


def copy_card_file() -> bool:
    """Copy the Lovelace card JS file to /config/www/. Called on startup."""
    return _copy_card_file()


def build_menu_points(yaml_path: str) -> frozenset[int]:
    """Read menu_structure.yaml and return every point_id referenced anywhere
    in the menu hierarchy as a frozenset.

    This is the single source of truth for the "menus" mode's point set.
    Called once at startup by generate_nibe_mqtt.main() and stored into
    nibe_entity_detection.MODES['menus'] before apply_mode() runs —
    so the enabled set and the dashboard cards are always derived from the
    same source and can never silently diverge.

    Returns an empty frozenset if the file cannot be read or parsed,
    so a missing YAML degrades gracefully rather than crashing startup.
    """
    try:
        with open(yaml_path, encoding='utf-8') as f:
            data = yaml.safe_load(f)
        points = _collect_menu_points(data.get('menus', []))
        log_startup.debug("Built MENU_POINTS from YAML: %d unique point_ids", len(points))
        return frozenset(points)
    except Exception as e:
        log_startup.warning("Could not build MENU_POINTS from %s: %s", yaml_path, e)
        return frozenset()


def provision_lovelace_ui(
    version: str,
    device_name: str,
    registry_watcher,
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
    entity_manager,
    registry_watcher,
    debug_mode: bool,
    lovelace_thread: threading.Thread | None = None,
) -> None:
    """Wire the debounced menu dashboard regeneration callback into entity_manager."""
    _wire_menu_dashboard_regen(entity_manager, registry_watcher, debug_mode,
                               lovelace_thread=lovelace_thread)


def teardown_lovelace() -> None:
    """Remove dashboard, resource registration, and card file on clean uninstall."""
    _teardown_lovelace()


def remove_menu_dashboard() -> None:
    """Remove the Nibe Menus dashboard if present. Idempotent; safe to call
    on every non-menus-mode startup — see _remove_menu_dashboard()."""
    _remove_menu_dashboard()
