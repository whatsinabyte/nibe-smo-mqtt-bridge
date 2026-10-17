"""
nibe_entity_detection.py
========================
Pure functions and lookup tables for classifying Nibe data points as
Home Assistant entity types and device classes.

Nothing in this module performs I/O, holds state, or imports from the
rest of the bridge.  All inputs are plain dicts / strings; all outputs
are strings or None.  This makes every function here trivially unit-testable.

Public surface
--------------
detect_entity_type(point)          → (entity_type, entity_category)
map_device_class(entity_type, unit, title) → str | None
get_value_mapping(point_id, point_data, register_type) → dict | None
get_entity_options(point_id, description)          → list[str]
mapping_status(point_id)                           → str | None
is_unitless_quantity(point_id, point)              → bool
firmware_fingerprint(metadata)                     → dict
get_register_type(point)           → "input" | "holding" | None
clean_string(text)                 → str
apply_divisor(raw, divisor)        → str
reverse_divisor(display, divisor)  → int
create_entity_id(point_id)        → str
parse_description_mapping(desc)   → dict | None
"""

import logging
import math
import re

log_detection = logging.getLogger("nibe.detection")


# ============================================================================
# ENTITY MODES
# ============================================================================
#
# Named groups of point IDs. "all" uses None as sentinel (replaced at runtime
# with the full discovered point list). "none" is an empty frozenset.

ESSENTIAL_POINTS = frozenset(
    {
        4,
        57,
        599,
        781,
        832,
        834,
        993,
        994,
        997,
        2491,
        2494,
        2495,
        2496,
        2497,
        2505,
        2766,
        2767,
        2792,
        3095,
        3096,
        3098,
        3170,
        3667,
        3671,
        4603,
        4604,
        5025,
        5033,
        5034,
        5035,
        5036,
    }
)

UPLINK_POINTS = ESSENTIAL_POINTS | frozenset(
    {
        1758,
        1708,
        121,
        2695,
        54,
        4651,
        4821,
        1838,
        2471,
        2472,
        2688,
        992,
        835,
        836,
        837,
        838,
        840,
        841,
        22268,
        842,
        843,
        845,
        3097,
        3353,
        2453,
        14987,
        2509,
        2527,
        1766,
        4084,
        3825,
        6138,
        6139,
        2506,
    }
)

ADVANCED_POINTS = UPLINK_POINTS | frozenset({8034, 1021, 6984, 3846, 3706, 4969, 4970})

# Points referenced in menu_structure.yaml — the full set needed to render
# the Nibe Menus dashboard. This is the "menus" mode's point set; enabling
# happens via EntityManager.apply_mode(), same as every other mode.
#
# Set to None here as a sentinel — populated at startup from menu_structure.yaml
# by generate_nibe_mqtt.main() calling nibe_lovelace.build_menu_points() and
# storing the result into MODES['menus']. This eliminates the dual-maintenance
# problem where the frozenset and the YAML could silently diverge, causing
# dashboard cards for points that were never enabled (Spook ghosts) or
# enabled points with no corresponding dashboard card.
#
# Tests that need a concrete set patch MODES['menus'] directly.
MENU_POINTS: frozenset[int] = frozenset()  # populated at startup — do not use at import time

MODES = {
    "essential": ESSENTIAL_POINTS,
    "monitoring": UPLINK_POINTS,
    "advanced": ADVANCED_POINTS,
    "menus": MENU_POINTS,  # also gates Lovelace menu dashboard provisioning
    "all": None,  # sentinel — replaced with full point list at runtime
    "none": frozenset(),
}


# ============================================================================
# STATIC LOOKUP TABLES
# ============================================================================

VALUE_MAPPINGS: dict[str, dict[int, dict]] = {
    "input": {
        # ── Entries below have NO description field in the firmware ───────────
        # All other enum mappings are parsed dynamically from the firmware's
        # own description field (e.g. "0 = Off, 1 = On") by parse_description_mapping().
        # Only registers where the firmware provides no description are hardcoded here.
        # Priority / operating mode — no firmware description
        1758: {10: "Off", 20: "Hot water", 30: "Heating", 40: "Pool", 60: "Cooling"},
        1762: {10: "Off", 20: "Opening", 30: "Closing"},
        1763: {10: "Off", 20: "Opening", 30: "Closing"},
        1764: {0: "Inactive", 10: "Off", 20: "Opening", 30: "Closing"},
        1765: {10: "Off", 20: "Opening", 30: "Closing"},
        1766: {10: "Off", 20: "Active", 30: "Passive", 40: "Opening", 50: "Closing"},
        # Operating mode PV panels — no firmware description. The same
        # four-state encoding as the SG Ready status (3260); labels as
        # yozik04/nibe PR #325 maps this register for an SMO S40 on 4.13.12.
        # 10 and 40 observed on hardware (issue #29: PV steering off / on);
        # 20 and 30 not yet observed.
        1021: {10: "Normal", 20: "Block", 30: "Low price", 40: "Boost"},
        # Operating mode (Smart Price Adaption) — no firmware description.
        # 10/30 confirmed empirically across two independent installations:
        # GitHub issue #35 reported 10 with SPA confirmed off (point 4789 read
        # 0), and a separate installation with SPA confirmed active (point
        # 4789 read 1) observed 30. See GitHub issue #35 and #29 for the same
        # multiple-of-ten encoding on a different register (1021/579).
        3292: {10: "Off", 30: "On"},
        # Frost protection heat exchanger, heat pumps 1-8 — no firmware
        # description. Mapping inferred by analogy with this same
        # manufacturer's outdoor-unit "Defrost" registers (e.g. modbus
        # registers 1763/1777/1791/1805), which share the identical 0/1/2
        # domain and are officially documented as Off/Active/Passive — not
        # independently confirmed against these specific registers. See
        # GitHub issue #35.
        632: {0: "Off", 1: "Active", 2: "Passive"},
        633: {0: "Off", 1: "Active", 2: "Passive"},
        634: {0: "Off", 1: "Active", 2: "Passive"},
        635: {0: "Off", 1: "Active", 2: "Passive"},
        636: {0: "Off", 1: "Active", 2: "Passive"},
        637: {0: "Off", 1: "Active", 2: "Passive"},
        638: {0: "Off", 1: "Active", 2: "Passive"},
        2804: {0: "Off", 1: "Active", 2: "Passive"},
        # Protection mode (EB101) — no firmware description. Labels as the
        # outdoor unit's defrost registers (3098 for EB101) have them, and as
        # a closed yozik04/nibe PR (#231) proposed for this register; not
        # observed (the reference installation reads 0).
        3097: {0: "Off", 1: "Active", 2: "Passive"},
        # ACS (Active Cooling System) — no firmware description
        2701: {3: "Passive", 7: "Active"},
        2702: {10: "Off", 20: "Opening", 30: "Closing"},
        2703: {10: "Off", 20: "Opening", 30: "Closing"},
        # On/Off status — no firmware description
        1838: {0: "Off", 1: "On"},
        2045: {0: "Closed to pool", 1: "Open to pool"},
        2046: {0: "Closed to pool", 1: "Open to pool"},
        3141: {0: "Off", 1: "On"},
        3146: {0: "Closed", 1: "Open"},
        3147: {0: "Off", 1: "On"},
        3149: {0: "Off", 1: "On"},
        3151: {0: "Off", 1: "On"},
        22077: {0: "Off", 1: "On"},
        # Alarm — no firmware description
        1709: {0: "No alarm", 1: "Active alarm"},
        # Operating mode SG Ready (register 1911): the read-only counterpart
        # of 10614, in the multiple-of-ten encoding. Labels as the yozik04/
        # nibe library maps this register for the S1155/S1255 and
        # S1156/S1256 (its extensions.json). Two values are measured on
        # hardware (VVM S320, GitHub issue #40): 10614 = Standard -> 10, and
        # Encouraged -> 30 — the library's Normal Price and Low Price. High
        # Price (20) and Free Electricity (40) aren't measured: the library
        # has them, and so does its open PR #325 from an SMO S40 owner on
        # 4.13.12 (as Block / Boost) — MAPPING_STATUS marks it partial.
        3260: {10: "Normal Price", 20: "High Price", 30: "Low Price", 40: "Free Electricity"},
        # All sub units operating prio (an input register). Values as observed
        # on a live controller, in step with its other status entities;
        # hot water and pool weren't observable there and stay raw numbers.
        56150: {0: "Idle", 1: "Heating", 2: "Cooling"},
        # Labels from the myUplink cloud API's own option list for these
        # points (enumValues), which uses the same point ids. 10897 and
        # 14952 aren't in the reference dump: they appear only under
        # conditions not met when it was taken.
        55000: {
            10: "Off",
            20: "Hot water",
            25: "Ext. hot water",
            30: "Heating",
            40: "Pool",
            41: "Pool 2",
            50: "Transfer",
            60: "Cooling",
        },  # Priority
        10897: {-1: "Calibration", 0: "Off", 1: "On"},  # Charge pump (EB101-GP12)
        14952: {
            0: "Off",
            1: "Heating",
            2: "Cooling",
            3: "Hot water",
            4: "Pool",
            5: "Pool 2",
            6: "Preheating",
        },  # Operating priority (EB101-EP14)
    },
    "holding": {
        # Language selection — ordering reflects Nibe's market priority. The
        # names are spelled as the controller's own menu shows them (checked
        # on a live controller): Český, Russkiy, Ukrayinska and Bǔlgarski are
        # NIBE's spellings, not the languages' usual self-names.
        #
        # MULTI-PLACE UPDATE: this is the controller's own hand-verified list
        # of firmware-supported languages (Nibe documents none of this — each
        # entry was confirmed against a real device's select entity). The
        # `language` config option's dropdown in config.yaml's schema (used
        # to set the REST API's Accept-Language header, a separate feature
        # from this select entity) is a hand-maintained BCP-47-code mirror of
        # this exact list. Adding a language here (new firmware release, user
        # request) means also adding its BCP-47 code to config.yaml's
        # `language` schema line — config.yaml is static and cannot derive
        # its dropdown from this Python dict at runtime, so the two must be
        # kept in sync by hand, in both directions.
        3745: {
            0: "English",
            1: "Svenska",
            2: "Deutsch",
            3: "Français",
            4: "Español",
            5: "Suomi",
            6: "Lietuvių",
            7: "Český",
            8: "Polski",
            9: "Nederlands",
            10: "Norsk",
            11: "Dansk",
            12: "Eesti",
            13: "Latviešu",
            14: "Russkiy",
            15: "Italiano",
            16: "Magyar",
            17: "Slovenčina",
            18: "Türkçe",
            19: "Hrvatski",
            20: "Română",
            21: "Íslenska",
            22: "Slovenščina",
            23: "Ukrayinska",
            24: "Bǔlgarski",
        },
        3976: {
            0: "Own setting",
            1: "Radiator",
            2: "Underfloor heating",
            3: "Radiator & Underfloor heating",
        },
        3751: {0: "Auto", 1: "Manual", 2: "Additional heat only"},
        4651: {0: "Intermittent", 1: "Auto"},
        4821: {0: "Intermittent", 1: "Auto"},
        4729: {0: "Intermittent", 1: "Auto"},
        4778: {0: "Intermittent", 1: "Auto"},
        # No firmware description for these either: options as the
        # controller's own menu lists them (language English), in that order,
        # verified on a live controller. Without a mapping they showed as bare
        # 0–2 numbers, and 4692/4085 as switches.
        4692: {0: "Target temp", 1: "Delta temp"},  # Charging method
        4085: {0: "Linear", 1: "Binary"},  # Internal additional heat stepping mode
        5482: {0: "Intermittent", 1: "Continuous", 2: "10 days cont."},  # Operating mode
        7022: {0: "Level monitor", 1: "Blocked", 2: "Off"},  # Blocking actions (ERS 3)
        7023: {0: "Level monitor", 1: "Blocked", 2: "Off"},  # Blocking actions (ERS 4)
        23141: {0: "Level monitor", 1: "Blocked", 2: "Off"},  # Blocking actions (ERS 5)
        23142: {0: "Level monitor", 1: "Blocked", 2: "Off"},  # Blocking actions (ERS 6)
        23143: {0: "Level monitor", 1: "Blocked", 2: "Off"},  # Blocking actions (ERS 7)
        23144: {0: "Level monitor", 1: "Blocked", 2: "Off"},  # Blocking actions (ERS 8)
        # Requested operating mode (SG Ready) — no firmware description.
        # Numeric order confirmed tested on real hardware (firmware 4.12.8,
        # S1256) via the Home Assistant community: "You set that to 0-3 for
        # high price/normal/low price/free electricity modes." Labels here
        # use NIBE's own official SG Ready terminology (product page: the
        # four states are Standard/Encouraged/Ordered/Cut off) rather than
        # that informal phrasing — 0=high price maps to Cut off (reduce
        # usage, discouraged), 1=normal maps to Standard, 2=low price maps
        # to Encouraged, 3=free electricity maps to Ordered (full power).
        # A previous VALUE_MAPPINGS entry for this same 4-state SG Ready
        # concept was mistakenly filed under point 3292 (Smart Price
        # Adaption, a different feature) with this exact domain rotated by
        # one position — removed in the same release this entry was added.
        # See GitHub issue #35.
        10614: {0: "Cut off", 1: "Standard", 2: "Encouraged", 3: "Ordered"},
        # Point 3260, the read-only SG Ready status: see the "input" table.
        # Heat pump type codes (best-effort, not officially documented)
        2471: {
            17: "S2125-12",
            16: "S2125-8",
            18: "S2125-16",
            30: "F2040-6",
            31: "F2040-8",
            32: "F2040-12",
            33: "F2040-16",
            40: "SMO S40",
        },
        2527: {
            17: "S2125-12",
            16: "S2125-8",
            18: "S2125-16",
            30: "F2040-6",
            31: "F2040-8",
            32: "F2040-12",
            33: "F2040-16",
            40: "SMO S40",
            55: "S1155",
        },
        4790: {0: "Off", 1: "Comfort", 2: "Saving", 3: "Saving PLUS"},
        # Smart Energy Source control method
        5269: {0: "Price per kWh", 1: "CO2"},
        # Time format
        3933: {0: "12h", 1: "24h"},
    },
}

# How far a VALUE_MAPPINGS entry has been checked, published as the static
# `mapping` entity attribute and shown in the Entity Manager card, which
# asks owners to report what their controller shows for the last two:
#   confirmed — every value matched against a live controller, or stated
#               by the firmware's own description
#   partial   — some values confirmed, the others inferred or not known yet
#               (an unknown value shows as a plain number)
#   inferred  — no value confirmed: taken by analogy, from documentation,
#               or of unrecorded origin (the initial commit kept no sources)
# Only points whose labels Home Assistant actually shows are listed — sensors
# and selects. The 0/1 mappings of binary sensors (1838, 2045, 2046, 3141,
# 3146, 3147, 3149, 3151, 22077, 1709) are left out: HA shows on/off there.
MAPPING_STATUSES = ("confirmed", "inferred", "partial")
MAPPING_STATUS: dict[int, str] = {
    # Options read off a live controller's own menu (language English).
    3745: "confirmed",
    4085: "confirmed",
    4692: "confirmed",
    5482: "confirmed",
    7022: "confirmed",
    7023: "confirmed",
    23141: "confirmed",
    23142: "confirmed",
    23143: "confirmed",
    23144: "confirmed",
    # Both values observed on hardware: 3292 on two installations (issue #35).
    3292: "confirmed",
    # SG Ready request: the 0-3 order tested on real hardware (issue #35).
    10614: "confirmed",
    # The firmware's own description gives the order (0 = price).
    5269: "confirmed",
    # 0/1/2 observed on a live controller; hot water and pool not yet.
    56150: "partial",
    # Labels from the myUplink cloud API (enumValues). Seen on a live
    # controller, in step with 56150 and 1758: 55000 30 (Heating) and 60
    # (Cooling), 14952 1 (Heating), 10897 1 (On, the pump running).
    55000: "partial",
    10897: "partial",
    14952: "partial",
    # PV panels operating mode: 10 and 40 observed in issue #29 (ten
    # transitions over six days); 20 and 30 from yozik04/nibe PR #325.
    1021: "partial",
    # SG Ready status: 10 and 30 measured (issue #40); 20 and 40 from the
    # yozik04/nibe library's mapping of the same register.
    3260: "partial",
    # Heat pump types: 17 (S2125-12) and 40 (SMO S40) read on a live
    # controller, 55 on an S1155's own module EB100 (GitHub issue #82);
    # the other model codes are best-effort.
    2471: "partial",
    2527: "partial",
    # Priority: Off (10), Heating (30) and Cooling (60) confirmed against
    # 56150 over a day of a live controller's history, every change in step;
    # Hot water (20) and Pool (40) not observable there.
    1758: "partial",
    # Frost protection, heat pumps 1-8: labels borrowed from the outdoor
    # unit's defrost registers, which share the 0/1/2 domain (CHANGELOG
    # 1.1.5). 2 = Passive confirmed on a live controller's menu 7.3.2.1 for
    # heat pump 1 (2804); the other seven are the same register family on
    # the same firmware. Off and Active not yet observed (Active is expected
    # in frost).
    632: "partial",
    633: "partial",
    634: "partial",
    635: "partial",
    636: "partial",
    637: "partial",
    638: "partial",
    2804: "partial",
    # Settings read on a live controller, HA and the controller's menu
    # agreeing. Two-option ones are confirmed by one value: 4651 = 1 = Auto
    # and 4821 = 0 = Intermittent, the same Intermittent/Auto mapping as
    # 4729/4778, and 3933 = 1 = 24h. The manual lists "Auto, Intermittent",
    # the reverse of the values — a manual's listing order is not the
    # value order.
    3933: "confirmed",
    4651: "confirmed",
    4729: "confirmed",
    4778: "confirmed",
    4821: "confirmed",
    # Every option selected on a live controller and matched in HA: SPA
    # "Affect heating" (menu 4.2.5), Operating mode (menu 4.1) and Flow
    # setting climate system (menu 7.1.6.2).
    4790: "confirmed",
    3751: "confirmed",
    3976: "confirmed",
    # Protection mode (EB101): defrost-family labels, unobserved.
    3097: "inferred",
    # Shunt and ACS states from the initial commit, which recorded no
    # evidence.
    1762: "inferred",
    1763: "inferred",
    1764: "inferred",
    1765: "inferred",
    1766: "inferred",
    2701: "inferred",
    2702: "inferred",
    2703: "inferred",
}


# Unitless sensors that are quantities — counts, sizes, speeds, raw sensor
# readings — graphed in Home Assistant as numbers (state class measurement).
# An allow-list, kept by id so it holds in every firmware language: the other
# unitless sensors are mostly codes (operating modes, statuses, alarm numbers,
# relay bitmasks), which read better as coloured state bars, and an unknown
# point keeps the bars too. The spot prices (26817–26840) are left out: their
# format is undocumented and "no price" is the register maximum.
UNITLESS_QUANTITY_POINTS: frozenset[int] = frozenset(
    {
        # Available / docked / used compressors, per use.
        *range(666, 671),
        671,  # ... external control (S1155)
        2784,  # Docked compressors external control (S1155)
        1999,
        2000,
        2001,
        2059,
        2882,
        2706,
        2707,
        2708,
        2709,
        2729,
        # Active heat pumps
        2024,
        # Compressor size (EB101, EB100)
        2472,
        2528,
        # Compressor, number of starts (EB101, EB100; 1959 "Compressor
        # starter" on ground-source models, GitHub issue #82)
        2505,
        2561,
        1959,
        # BP4, unprocessed — per heat pump (EB100–EB108) and module (EP14/EP15)
        2883,
        2897,
        2911,
        2925,
        2939,
        2953,
        2967,
        2981,
        2995,
        3037,
        3051,
        3065,
        3079,
        3093,
        3107,
        3121,
        # Low-pr. sens, unprocessed
        3251,
        3254,
        3257,
        # fan speed
        3354,
        3357,
        3360,
        # Ext. add. heat active steps
        6717,
    }
)


# Running totals that only go up — operating hours and compressor starts —
# published with state class total_increasing, so a statistics graph can
# show the increase per day, week or month. Kept by id, like
# UNITLESS_QUANTITY_POINTS. Countdowns and per-program timers (time to
# start, floor drying ongoing time) are not totals and stay measurements.
TOTAL_INCREASING_POINTS: frozenset[int] = frozenset(
    {
        # Compressor, total time pool / pool 2 / cooling, per heat pump and module
        555,
        556,
        558,
        559,
        561,
        562,
        564,
        565,
        567,
        568,
        570,
        571,
        573,
        574,
        576,
        577,
        579,
        580,
        582,
        583,
        585,
        586,
        588,
        589,
        591,
        592,
        594,
        595,
        597,
        598,
        599,
        600,
        601,
        # Total op. time compr. cooling
        605,
        # Compressor, total time pool / pool 2, main unit (EP14)
        606,
        607,
        # Total run time additional heat
        1755,
        # On ground-source models (S1155, GitHub issue #82): compressor
        # starts, total and hot water run time, additional heat run time
        # for hot water.
        1959,
        1961,
        1963,
        1865,
        # Compressor, number of starts (EB101-EP14, EB100-EP14)
        2505,
        2561,
        # Compressor, oper. time, total / hot water (EB101-EP14, EB100-EP15, EB100-EP14)
        2506,
        2507,
        2544,
        2545,
        2562,
        2563,
        # Compressor, total time energy storage, main unit and heat pumps 1–8
        *range(25671, 25689),
    }
)


def is_unitless_quantity(point_id: int, point: dict) -> bool:
    """True for a UNITLESS_QUANTITY_POINTS sensor that has no value labels —
    a labelled sensor's state is text, and a state class would make HA
    reject every state as non-numeric."""
    return point_id in UNITLESS_QUANTITY_POINTS and get_value_mapping(point_id, point) is None


def mapping_status(point_id: int) -> str | None:
    """Return the MAPPING_STATUS entry for a point, or None — also for an
    entry that isn't one of MAPPING_STATUSES, so a typo in the table never
    reaches Home Assistant as an attribute value."""
    status = MAPPING_STATUS.get(point_id)
    return status if status in MAPPING_STATUSES else None


# Per-point entity type overrides — always wins over auto-detection.
# Key: point_id  Value: target HA entity type
#
# binary_sensor entries here are only needed for points that cannot be
# auto-detected by _is_auto_binary_sensor() — i.e. non-INPUT registers,
# non-u8 sizes, or writable points that happen to behave as on/off flags.
# INPUT register u8 status flags are classified automatically.
ENTITY_TYPE_OVERRIDES: dict[int, str] = {
    # ── Holding register shape corrections ────────────────────────────────────
    # Auto-detect uses register type + shape heuristics and gets these wrong:
    3478: "button",  # Reset alarm — trigger-only, not a number
    8052: "button",  # Start fan de-icing — trigger-only, not a number
    12392: "switch",  # Show outdoor temperature — 0/1 but auto-detects as number
    12393: "switch",  # Show indoor temperature — 0/1 but auto-detects as number
    3706: "switch",  # Periodic increase activated — persistent on/off, not a number
    8982: "switch",  # Away mode — max=0 in metadata (firmware quirk) so auto-detects as number
    3754: "switch",  # Activate forced control — same firmware quirk
    8060: "switch",  # Defrost requested (EB101) — 0–1 holding register, but s8, so auto-detects as number
    # Automatic speed, heating medium pump GP1 (menu 7.1.2.2, on/off in the
    # S1155 manual): 0–1, but declared with unit "%", so auto-detects as a
    # 0–1 % number (ground-source models, GitHub issue #82).
    4798: "switch",  # ... hot water
    4799: "switch",  # ... heating
    4800: "switch",  # ... pool
    # ── MODBUS_NO_REGISTER — would fall through to sensor/diagnostic without override ──
    32824: "switch",  # Power limitation activation
    # ── THS-10 accessory — auto-detects as number without override ────────────
    5110: "switch",  # Prevent condensation climate system 1
    5214: "switch",  # Limit humidity in the room, cooling climate system 1
    # ── Time-of-day registers — stored as seconds, shown as HH:MM ────────────
    3708: "time",  # Periodic increase start time
    12401: "time",  # HW circulation start time period 1
    12402: "time",  # HW circulation start time period 2
    12403: "time",  # HW circulation stop time period 1
    # ── Circulation pump (EB101) — auto-detects as switch (0/1 shape) ─────────
    4562: "switch",  # Manual heating medium pump speed (0=auto, 1=manual)
    # ── Binary sensors — values from firmware description ─────────────────────
    1820: "sensor",  # External block­ing
    1827: "sensor",  # Step controlled add. heat blocking
    2002: "sensor",  # Diver­ter valve hot water (QN10)
    # ── binary_sensor — non-INPUT or non-standard shape, cannot be auto-detected ──
    # Point 22077 is an s16 holding register, so _is_auto_binary_sensor() skips it.
    22077: "binary_sensor",  # AUX from Modbus
}

# Entity types that belong in the HA "config" category (writable controls).
CONFIG_ENTITY_TYPES: list[str] = ["switch", "number", "select", "button", "text", "time"]

# Per-point unit overrides.
# Key: point_id  Value: replacement unit string sent to HA.
UNIT_OVERRIDES: dict[int, str] = {
    4562: "",  # switch (0=auto, 1=manual) — firmware wrongly reports unit='%'
    50825: "%",  # THS-10 accessory point — firmware reports no unit but value is %
    50827: "%",  # Humidity: ths-10 — firmware unit is "%RH", which HA's auto-detection rejects
    # variableType=date registers — point 2685 ("Date, periodic hot water")
    # is the only known instance.  The raw integerValue (e.g. 5265) is
    # suspected to be days since 2010-01-01 (5265 → 2024-06-01 is consistent
    # with one live installation), but this is unverified.  Exposed as a plain
    # sensor showing the raw integer until the encoding is confirmed.
    # variableType=time registers — formerly exposed as number with "s" unit.
    # Now exposed as HA time entities (HH:MM:SS), no unit needed.
    # Unit overrides below are intentionally removed; see ENTITY_TYPE_OVERRIDES.
    # Seconds of blank time left, charge pumps 1–8 (818–821 are pumps 8–5;
    # they used to be missing, so only pumps 1–4 showed a unit).
    818: "s",
    819: "s",
    820: "s",
    821: "s",
    822: "s",
    823: "s",
    824: "s",
    825: "s",
    1205: "DM",
    1206: "DM",
    1207: "DM",
    1208: "DM",
    1209: "DM",
    1210: "DM",
    1211: "DM",
    1212: "DM",
    1213: "DM",
    1214: "DM",
    1215: "DM",
    1216: "DM",
    1217: "DM",
    1218: "DM",
    1219: "DM",
    # DM start source, priority 1–5 smart energy source — the settings for
    # the start DM values above, which showed no unit.
    5294: "DM",
    5295: "DM",
    5296: "DM",
    5297: "DM",
    5298: "DM",
    # Firmware reports no unit; the unit is the one the installer menus give
    # (menu_structure.yaml), each one Home Assistant recognises.
    995: "bar",  # Pressure sensor, injection (EB101-BP11)
    849: "%",  # Degree of opening EEV (EB101)
    3282: "°C",  # Pool offset (SPA)
    3283: "°C",  # Cooling offset (SPA)
    4529: "°C",  # Cooling/heating sensor set point
    4685: "°C",  # Hot water compressors step difference
    3861: "h",  # Floor drying ongoing time
    4030: "min",  # More hot water, minutes remaining
    14314: "W",  # Available power (Solar PV, via Modbus)
}

# Per-point device_class overrides.
# Empty for now: its last entries (25165/25166, forced to "power") were
# removed once firmware started reporting the correct unit ('kW') itself,
# since map_device_class() already infers "power" unassisted from that
# unit via _UNIT_TO_DEVICE_CLASS. Kept as a mechanism for the next firmware
# metadata mistake that needs correcting this way.
DEVICE_CLASS_OVERRIDES: dict[int, str] = {}

# Per-point divisor overrides — corrects a firmware self-reported divisor
# confirmed physically wrong, as opposed to UNIT_OVERRIDES above (which
# only corrects a displayed unit label, not the actual scale of the value).
DIVISOR_OVERRIDES: dict[int, int] = {
    # Production (PV Power): firmware declares divisor=1 (i.e. "the raw
    # value is directly in kW"), but the real physical scale is 10 W per
    # raw unit (raw x 10 = Watts = raw / 100 kW) — confirmed on a VVM S320
    # by comparing a written value against myUplink's own display for the
    # same PV inverter (GitHub issue #84): writing 17 (intended as 170 W)
    # showed as "17 kW" in HA while myUplink correctly read ~0.2 kW for the
    # same moment. Also declared divisor=1 on this project's own SMO S40
    # reference dump, so the bug is in the firmware's self-reported
    # metadata itself, not specific to one controller model.
    29258: 100,
}


def apply_divisor_override(point_id: int, metadata: dict) -> dict:
    """Return metadata with a corrected divisor for a point in
    DIVISOR_OVERRIDES, or the same dict unchanged otherwise.

    Must be applied wherever a fresh metadata dict enters the system —
    point discovery (EntityManager._index_point) and every bulk-fetch poll
    (EntityManager._fetch_bulk_data) — since the firmware resends its own
    (wrong) divisor on every single response. There is no one place
    downstream where patching a cached copy would stick; both entry points
    call this on every metadata dict they receive instead.
    """
    override = DIVISOR_OVERRIDES.get(point_id)
    if override is None or metadata.get("divisor") == override:
        return metadata
    return _patch_metadata(metadata, {"divisor": override})


# Key under which a patched metadata dict keeps the firmware's own values of
# the fields an override replaced, so firmware_fingerprint() can still tell
# when a firmware update changes them (e.g. NIBE fixing 3702's minimum).
FIRMWARE_ORIGINAL_KEY = "firmware_original"

# The fields of a point's metadata that describe the register itself: only a
# firmware update changes them. Titles depend on the language option and
# values change all the time, so neither is part of the fingerprint.
FINGERPRINT_FIELDS = (
    "modbusRegisterType",
    "modbusRegisterID",
    "variableSize",
    "minValue",
    "maxValue",
    "divisor",
    "unit",
)


def _patch_metadata(metadata: dict, changes: dict) -> dict:
    """A copy of metadata with changes applied, recording the replaced
    firmware values under FIRMWARE_ORIGINAL_KEY (the first override of a
    field wins, so re-applying an override keeps the firmware's value)."""
    originals = dict(metadata.get(FIRMWARE_ORIGINAL_KEY) or {})
    for field in changes:
        originals.setdefault(field, metadata.get(field))
    return {**metadata, **changes, FIRMWARE_ORIGINAL_KEY: originals}


def firmware_fingerprint(metadata: dict) -> dict:
    """The firmware's own FINGERPRINT_FIELDS for a point, with any value an
    override replaced put back."""
    fingerprint = {field: metadata.get(field) for field in FINGERPRINT_FIELDS}
    fingerprint.update(metadata.get(FIRMWARE_ORIGINAL_KEY) or {})
    return fingerprint


# Per-point raw minValue/maxValue overrides (None keeps the firmware's own),
# for a declared bound confirmed wrong — same reason as DIVISOR_OVERRIDES:
# it feeds both HA's number range and write_point's own range check.
RANGE_OVERRIDES: dict[int, tuple[int | None, int | None]] = {
    # Stop temperature HW periodic increase (the anti-legionella cycle):
    # declares minValue 55 with divisor 10, i.e. 5.5 °C — a factor of ten
    # below the real 55 °C minimum (installer menu range 55 – 70 °C; the
    # live value in every reference dump is raw 550, that very minimum).
    3702: (550, None),
}


def apply_metadata_overrides(point_id: int, metadata: dict) -> dict:
    """apply_divisor_override plus RANGE_OVERRIDES — call this wherever a
    fresh metadata dict enters the system (see apply_divisor_override)."""
    metadata = apply_divisor_override(point_id, metadata)
    bounds = RANGE_OVERRIDES.get(point_id)
    if bounds is None:
        return metadata
    lo, hi = bounds
    if (lo is None or metadata.get("minValue") == lo) and (
        hi is None or metadata.get("maxValue") == hi
    ):
        return metadata
    changes = {}
    if lo is not None:
        changes["minValue"] = lo
    if hi is not None:
        changes["maxValue"] = hi
    return _patch_metadata(metadata, changes)


# Unit → HA device_class lookup (after _UNIT_NORMALISE has been applied).
_UNIT_TO_DEVICE_CLASS: dict[str, str] = {
    "°C": "temperature",
    "°F": "temperature",
    "K": "temperature",
    "Wh": "energy",
    "kWh": "energy",
    "MWh": "energy",
    "GWh": "energy",
    "TWh": "energy",
    "mW": "power",
    "W": "power",
    "kW": "power",
    "MW": "power",
    "GW": "power",
    "TW": "power",
    "mVA": "apparent_power",
    "VA": "apparent_power",
    "kVA": "apparent_power",
    "mvar": "reactive_power",
    "var": "reactive_power",
    "kvar": "reactive_power",
    "mA": "current",
    "A": "current",
    "µV": "voltage",
    "mV": "voltage",
    "V": "voltage",
    "kV": "voltage",
    "MV": "voltage",
    "Hz": "frequency",
    "kHz": "frequency",
    "MHz": "frequency",
    "GHz": "frequency",
    "cbar": "pressure",
    "mbar": "pressure",
    "bar": "pressure",
    "Pa": "pressure",
    "hPa": "pressure",
    "kPa": "pressure",
    "mPa": "pressure",
    "mmHg": "pressure",
    "inHg": "pressure",
    "psi": "pressure",
    "%RH": "humidity",
    "mL/s": "volume_flow_rate",
    "L/s": "volume_flow_rate",
    "L/min": "volume_flow_rate",
    "L/h": "volume_flow_rate",
    "l/h": "volume_flow_rate",
    "m³/s": "volume_flow_rate",
    "m³/min": "volume_flow_rate",
    "m³/h": "volume_flow_rate",
    "s": "duration",
    "ms": "duration",
    "µs": "duration",
    "min": "duration",
    "h": "duration",
    "m/s": "wind_speed",
    "km/h": "wind_speed",
    "mph": "wind_speed",
    "kn": "wind_speed",
}

# Firmware unit variants → canonical HA unit strings.
_UNIT_NORMALISE: dict[str, str] = {
    "days": "d",
    # Firmware mojibake: Latin-1 '°' mis-decoded as UTF-8 produces 'Â°'
    "Â°C": "°C",
    "Â°F": "°F",
    "Â°": "°",
}

# Unit strings for which HA has no valid device_class.
_UNCLASSIFIABLE_UNITS: frozenset[str] = frozenset({"%", "DM", "rpm"})

# Title keyword → device_class rules (language-agnostic hardware codes only).
_SENSOR_KEYWORD_RULES: list[tuple] = [
    (
        (
            "bt1",
            "bt2",
            "bt3",
            "bt4",
            "bt6",
            "bt7",
            "bt10",
            "bt11",
            "bt12",
            "bt14",
            "bt16",
            "bt17",
            "bt20",
            "bt21",
            "bt22",
            "bt23",
            "bt24",
            "bt25",
            "bt26",
            "bt27",
            "bt28",
            "bt29",
            "bt50",
            "bt51",
            "bt57",
            "bt64",
            "bt65",
            "bt70",
            "bt71",
            "bt75",
            "bt76",
            "bt77",
            "bt81",
            "bt82",
            "bt83",
            "bt84",
        ),
        "temperature",
    ),
    (("bp1", "bp2", "bp3", "bp4", "bp5", "bp6", "bp7", "bp8", "bp9", "bp10", "bp11"), "pressure"),
]

# Cache for parse_description_mapping — firmware description strings are
# static for the bridge's lifetime.  Capped at 2000 entries to prevent
# unbounded growth on installations with very large register sets.
_description_mapping_cache: dict[str, dict | None] = {}
_DESCRIPTION_CACHE_MAX = 2000

# Point IDs for which an ENTITY_TYPE_OVERRIDES warning has already been
# logged this process lifetime.  Same one-shot-per-startup pattern as
# MqttDiscoveryPublisher._range_warnings_issued / _unit_override_warnings_issued
# in nibe_mqtt_publisher.py — detect_entity_type() is a pure module-level
# function with no instance to hold this state, so it lives at module level
# instead.  Capped defensively, though a real installation has at most a
# few thousand distinct points so this is unlikely to matter in practice.
_entity_type_override_warnings_issued: set = set()
_ENTITY_TYPE_WARNING_CACHE_MAX = 2000


# ============================================================================
# STRING / VALUE UTILITIES
# ============================================================================


def clean_string(text: object) -> str | None:
    """Normalise a Nibe API string for use as an HA entity title.

    Strips stray quotes, non-breaking spaces, and UTF-8 mojibake bytes that
    appear in some firmware versions, then collapses internal whitespace.
    """
    if not text or not isinstance(text, str):
        # Deliberate passthrough for a falsy/non-str input (e.g. None) — the
        # caller relies on getting back exactly what it passed in rather than
        # a coerced value, so the declared `str | None` return type is a
        # best-effort description of the common case, not a hard guarantee.
        return text  # type: ignore[return-value]
    cleaned = text.strip()
    cleaned = (
        cleaned.replace("\u00c2", "").replace("\u00a0", " ").replace("\xa0", " ")
    )  # pragma: no mutate
    # U+00AD is the soft-hyphen inserted by Nibe firmware as an invisible
    # line-break hint.  It does not display but breaks substring search:
    # "exter\u00adn" does not match "extern".  Strip it entirely.  # pragma: no mutate
    cleaned = cleaned.replace("\u00ad", "")  # pragma: no mutate
    # Quote-stripping must run after mojibake removal above: a stray quote
    # next to a mojibake byte (e.g. '0"\u00c2') is not at the true string edge
    # until that byte is gone, so stripping quotes first can leave a
    # dangling quote that only a second clean_string() call would remove.
    cleaned = cleaned.strip().strip('"').strip("'")
    return " ".join(cleaned.split())


def clean_unit(unit: object) -> str:
    """Normalise a Nibe API unit string for display.

    Strips the same UTF-8 mojibake byte handled by clean_string (e.g.
    'Â°C' -> '°C' from a Latin-1 '°' mis-decoded as UTF-8), then applies
    _UNIT_NORMALISE for known full-string substitutions (e.g. 'days' ->
    'd'). This is the single source of truth for unit cleaning — earlier
    revisions had three independent ad-hoc implementations scattered
    across generate_nibe_mqtt.py (a direct '\\u00c2' strip in two places)  # pragma: no mutate
    and this module (a _UNIT_NORMALISE table lookup with no stripping),
    which could silently drift out of sync with each other. Always
    returns a string, never None, since an empty/missing unit is valid
    (many points are dimensionless).
    """
    if not unit or not isinstance(unit, str):
        return ""
    cleaned = (
        unit.strip().replace("\u00c2", "").replace("\u00a0", " ").replace("\xa0", " ")
    )  # pragma: no mutate
    cleaned = " ".join(cleaned.split())
    return _UNIT_NORMALISE.get(cleaned, cleaned)


def apply_divisor(raw_value: int, divisor: int) -> str:
    """Convert a raw Nibe integer to its display string by applying the divisor.

    If divisor is 1 (or 0, treated as 1) the raw integer is returned as-is.
    Otherwise the result is rounded to the number of decimal places implied
    by the divisor (divisor=10 → 1 dp, divisor=100 → 2 dp, etc.) to avoid
    floating-point representation noise like 20.000000000000004.

    ``divisor=0`` is treated as ``divisor=1`` (no scaling) rather than raising
    ZeroDivisionError.  The Nibe API spec defines divisor as an integer with no
    documented minimum; firmware versions have been observed to emit 0 for
    dimensionless registers.  Treating 0 as 1 is the correct defensive default:
    it preserves the raw integer value and never silently corrupts data.
    """
    # `divisor and` already excludes 0 (falsy), making the `!= 0` comparison
    # redundant — mutating it to any other constant, or to `or`, changes
    # nothing observable. Effective is always >1 whenever this line's decimal
    # formatting runs (guarded by `if effective == 1: return` above), so
    # max(0, ...) vs max(1, ...) never differs either. Both rstrip() calls
    # strip a character *set*; padding either set with extra characters that
    # never appear in the formatted string (e.g. 'X') is unobservable.
    # All verified empirically.
    effective = divisor if divisor and divisor != 0 else 1
    if effective == 1:
        return str(raw_value)
    decimal_places = max(0, math.ceil(math.log10(effective)))
    return f"{raw_value / effective:.{decimal_places}f}".rstrip("0").rstrip(".")


def reverse_divisor(display_value: float, divisor: int) -> int:
    """Convert a user-supplied display value back to a raw Nibe integer.

    HA number entities send the human-readable (post-divisor) value.
    Multiply back by divisor and round to int before writing to the API.
    ``divisor=0`` is treated as ``divisor=1`` — see ``apply_divisor`` for
    the full rationale.
    """
    # Same `divisor and` redundancy as apply_divisor above — the `!= 0`
    # comparison (and its operator) is unobservable. Verified empirically.
    effective = divisor if divisor and divisor != 0 else 1
    return round(display_value * effective)


def create_entity_id(point_id: int) -> str:
    """Return the stable MQTT entity ID for a Nibe point.

    Uses only the numeric point ID so topic paths are language-agnostic
    and permanent regardless of firmware title changes.
    """
    return f"nibe_{point_id}"


def get_register_type(point: dict) -> str | None:
    """Return 'input', 'holding', or None based on the point's Modbus register type."""
    # modbus_type is only ever checked via `in` against 'INPUT'/'HOLDING' —
    # any default that doesn't itself contain those substrings (e.g. '' or
    # 'XXXX') is unobservable. Verified empirically.
    modbus_type = point.get("metadata", {}).get("modbusRegisterType", "")
    if "INPUT" in modbus_type:
        return "input"
    if "HOLDING" in modbus_type:
        return "holding"
    return None


# ============================================================================
# DESCRIPTION / VALUE MAPPING
# ============================================================================


def _split_mapping_part(part: str) -> tuple[str, str] | None:
    """Split one comma-separated segment of a firmware enum description on
    its key/value separator.

    Real firmware descriptions are not consistent about which separator a
    given register family uses: most use '=' ("0 = Off, 1 = Active"), but
    some (observed across all 4 shipped language dumps — en/de/nl/sv) use
    ':' instead ("0: Zones, 1: Climate system, 2: Profile"), and at least
    one register (point 22268, "Successful pump run" result code) mixes
    both separators within the *same* description string:
    "0 = Successful, 1: Low supply, 2: Low return, ...". '=' is tried
    first per-segment (not per-whole-string) so a mixed description like
    that one parses every pair correctly instead of silently dropping the
    ':'-separated ones. Returns None if the segment has neither separator
    (e.g. free text like "Bit position for zone", which carries no enum
    mapping at all).
    """
    sep = "=" if "=" in part else ":" if ":" in part else None
    if sep is None:
        return None
    left, right = part.split(sep, 1)
    return left.strip(), right.strip()


_PLAIN_INT = re.compile(r"-?[0-9]+")


def _plain_int(text: str) -> int | None:
    """The integer a description key spells, or None. Only a plain decimal
    integer counts, as the firmware writes keys ("0 = Off", "-1 = ..."):
    int() also accepts "+5", "1_0" and non-ASCII digits, which made a label
    like "+5" in "+5 = 5" read as the key."""
    return int(text) if _PLAIN_INT.fullmatch(text) else None


def parse_description_mapping(description: str) -> dict | None:
    """Parse a Nibe firmware enum description string into a {int: str} mapping.

    Handles both orderings used by different register families:
      "0 = Off, 1 = Active"   (integer on the left)
      "Auto = 0, Manual = 1"  (label on the left)
    and both key/value separators seen in the wild ('=' and ':' — see
    _split_mapping_part).

    Returns None if no parseable pairs are found.
    Results are cached — firmware descriptions are static for the bridge's lifetime.
    """
    if not description or ("=" not in description and ":" not in description):
        return None

    if description in _description_mapping_cache:
        return _description_mapping_cache[description]

    mapping = {}
    for part in description.split(","):
        pair = _split_mapping_part(part.strip())
        if pair is None:
            continue
        left, right = pair
        key = _plain_int(left)
        if key is not None:
            mapping[key] = right
        elif (key := _plain_int(right)) is not None:
            mapping[key] = left

    result = mapping if mapping else None
    if len(_description_mapping_cache) < _DESCRIPTION_CACHE_MAX:
        _description_mapping_cache[description] = result
    return result


def _lookup_manual_mapping(point_id: int) -> dict | None:
    """Return VALUE_MAPPINGS[<any register type>][point_id], or None.

    Checks *every* register-type sub-table, not just a single one named by
    a caller-derived register_type — that value can come back None or wrong
    when the caller's metadata dict is momentarily incomplete (e.g. a
    dynamic point's very first poll), and callers may cache whatever they
    get back on the first non-None result. If an incomplete register_type
    were allowed to skip straight to an inferior fallback, that inferior
    result (e.g. literal "price"/"co2" instead of the manual table's "Price
    per kWh"/"CO2") could get permanently cached and never reconsidered even
    once full metadata becomes available on a later poll. point_id is
    unique across VALUE_MAPPINGS's register-type sub-tables, so checking all
    of them is unambiguous regardless of what register_type resolves to.
    """
    for table in VALUE_MAPPINGS.values():
        manual = table.get(point_id)
        if manual:
            return manual
    return None


def get_value_mapping(
    point_id: int,
    point_data: dict,
    register_type: str | None = None,
) -> dict[int, str] | None:
    """Return a {raw_int: label_str} mapping for a point, or None.

    Lookup order:
      1. _lookup_manual_mapping(point_id) — manual table (takes precedence).
      2. parse_description_mapping(point_data['description']) — firmware-provided enum.
    """
    manual = _lookup_manual_mapping(point_id)
    if manual:
        return manual

    # parse_description_mapping short-circuits to None for any falsy value
    # AND for any string without '=' — so None, '', or 'XXXX' (none contain
    # '=') all produce the identical result. Verified empirically.
    description = point_data.get("description", "")
    return parse_description_mapping(description)


def get_entity_options(
    point_id: int,
    description: str,
) -> list[str]:
    """Return the ordered list of option labels for a select entity.

    Returns an empty list when no options can be determined.
    """
    mapping = _lookup_manual_mapping(point_id)

    if mapping:
        return [text for _, text in sorted(mapping.items())]

    # The `or` here is required, not just an equivalent alternative to
    # `and`: a colon-only description (e.g. "0: Zones, 1: Climate system,
    # 2: Profile") contains no '=' at all, so `and` would wrongly reject
    # it. See _split_mapping_part for why both separators are supported.
    if "," in description and ("=" in description or ":" in description):
        options = []
        for part in description.split(","):
            pair = _split_mapping_part(part.strip())
            if pair is None:
                continue
            left, right = pair
            text = right if _plain_int(left) is not None else left
            if text and text not in options:
                options.append(text)
        if len(options) >= 2:
            return options

    return []


# ============================================================================
# ENTITY TYPE DETECTION
# ============================================================================


# Registers the bridge never writes, whatever their register type — DOCS.md
# "Intentionally Unexposed Registers" explains each group. Exposed as
# read-only sensors. This used to rest on the firmware's isWritable flag,
# which is not relied on (see is_writable_point) and never covered 55884 or
# the spot prices at all.
SAFETY_READ_ONLY_POINTS: frozenset[int] = frozenset(
    {
        # Block new compressor — responds only during an active system event.
        55749,
        # Set point value power — a direct compressor power request with no
        # firmware timeout, kept across power cycles.
        55884,
        # Spot price per hour, 00:00–24:00 — value format undocumented.
        *range(26817, 26841),
    }
)


def is_writable_point(metadata: dict, point_id: int | None = None) -> bool:
    """Whether a point accepts writes, judged by its register type alone.

    The firmware's own isWritable flag isn't relied on: it follows the
    register type everywhere except 17 holding registers flagged false —
    among them 3478 "Reset alarm", a trigger-only register, and 4064
    "Oper. mode", which has a value mapping — so a control the user could
    see was refused by the bridge before the controller was even asked.
    Input registers are read-only by definition; a holding register is
    writable, and the controller's own per-point write verdict ("modified"
    / "error: read only value", see write_point) has the final word. The
    one REST-only point (MODBUS_NO_REGISTER: 32824, an on/off switch) is
    treated as writable too; a point without a register type is not. Judged
    per REST point: an input and a holding register can share a Modbus
    register number.
    """
    if point_id in SAFETY_READ_ONLY_POINTS:
        return False
    return metadata.get("modbusRegisterType") in ("MODBUS_HOLDING_REGISTER", "MODBUS_NO_REGISTER")


def meaningful_default(
    metadata: dict, point_id: int | None = None, mapping: dict | None = None
) -> int | None:
    """A writable holding setting's factory default (raw), or None when it
    has none worth comparing against: no default, no declared range
    (min == max), or a 0 default at a 0 minimum with a wider range — the
    firmware's placeholder, not a documented default — unless *mapping*
    (a select's options) names 0. The same rule the Nibe Menus dashboard
    uses for "default: X" (_build_point_defaults)."""
    if not is_writable_point(metadata, point_id):
        return None
    if metadata.get("modbusRegisterType") != "MODBUS_HOLDING_REGISTER":
        return None
    min_val = metadata.get("minValue", 0)
    max_val = metadata.get("maxValue", 0)
    default = metadata.get("intDefaultValue")
    if min_val == max_val or not isinstance(default, int) or isinstance(default, bool):
        return None
    if mapping and default in mapping:
        return default
    if default == 0 and min_val == 0 and max_val > 1:
        return None
    return default


def is_switch_candidate(metadata: dict) -> bool:
    """Return True if a holding register has the signature of a boolean on/off switch."""
    return all(
        [
            metadata.get("modbusRegisterType") == "MODBUS_HOLDING_REGISTER",
            metadata.get("unit", "") == "",
            metadata.get("variableSize") == "u8",
            metadata.get("minValue", 0) == 0,
            # Any default not equal to 1 (e.g. None, dropped) is unobservable —
            # only ever compared with `== 1`. Verified empirically.
            metadata.get("maxValue", 0) == 1,
            metadata.get("divisor", 1) == 1,
        ]
    )


def is_number_candidate(metadata: dict) -> bool:
    """Return True if the register has a physical unit, implying a numeric measurement."""
    # `or ''` also covers an explicit "unit": null from the API — .get()'s
    # default only applies when the key is absent.
    return bool((metadata.get("unit", "") or "").strip())


# ── Auto-detection support for binary_sensor (INPUT registers only) ───────────
#
# INPUT register u8 points with min=0, max≤1 and no unit are
# almost always on/off status flags. We auto-detect them as binary_sensor rather
# than requiring every flag to be listed in ENTITY_TYPE_OVERRIDES.
#
# Points that match the shape but are NOT binary sensors are listed explicitly
# in _BINARY_SENSOR_EXCLUSIONS by point ID. This is unambiguous and immune to
# soft-hyphen or title-translation quirks.

_BINARY_SENSOR_EXCLUSIONS: frozenset[int] = frozenset(
    {
        # Fan / pump speed registers — numeric RPM values, not flags
        765,
        766,
        1495,
        3354,
        3357,
        3360,
        # Time-count registers
        818,
        819,
        820,
        821,
        822,
        823,
        824,
        825,
        # Numeric codes and indices
        832,  # Alarm number from outdoor air heat pump (EB101)
        1511,  # Serial index (EB101)
        # Equipment type / size — numeric enum values
        2471,
        2472,  # Heat pump type + compressor size (EB101)
        2527,
        2528,  # Heat pump type + compressor size (EB100)
        # Step count
        6717,  # Ext. add. heat active steps
        # Firmware version register (a version number, not a flag)
        14987,  # Version, inverter (EB101)
        # Compressor count registers — value is 0..N, not a binary flag
        666,
        667,
        668,
        669,
        670,  # Available compressors (heating/HW/pool/cooling)
        671,  # Available compressors external control (S1155, issue #82)
        2784,  # Docked compressors external control (S1155, issue #82)
        1999,
        2000,
        2001,
        2059,
        2882,  # Docked compressors
        2706,
        2707,
        2708,
        2709,
        2729,  # Used compressors
        # Multi-state registers that happen to share the binary shape
        1758,
        55000,  # Priority (8-state)
        55335,  # EEV Control Mode (EB101) — multi-state valve control
        1760,  # Operating mode internal add. heat (4-state: off/step1/step2/step3)
        # Operating mode PV panels — 2-state {10,40} value domain, so the
        # VALUE_MAPPINGS length check alone can't tell it apart from a real
        # boolean; excluded explicitly. See GitHub issue #29.
        1021,
        # Operating mode (Smart Price Adaption) — 2-state {10,30} value
        # domain, same shape as 1021 above; excluded explicitly for the same
        # reason. See GitHub issue #35.
        3292,
        # Oper. mode shunt climate system 5-8 — same multiple-of-ten
        # operating-mode encoding as 1758/1762-1766/3292, but the firmware
        # gives no description text and no confirmed value domain, so no
        # VALUE_MAPPINGS entry is added; published as a plain sensor with the
        # raw integer value. See GitHub issue #35.
        242,
        243,
        244,
        245,
        # Fan status (EB101-EP14) — same undocumented multiple-of-ten shape.
        998,
        # Relay status — a bitmask of individual relay on/off states packed
        # into one integer (this manufacturer's step-controlled additional
        # heat can combine relays in on/off patterns for "binary stepping" —
        # see menu_structure.yaml), not a single boolean flag. modbusRegisterID
        # is 0 (unlike every other point), suggesting this is a synthetic
        # aggregate rather than one real Modbus register. Published as a
        # plain sensor with the raw integer; splitting it into individual
        # relay states is left to an HA-side template, since the bridge has
        # no way to know how many relays exist or what each bit means on a
        # given installation. See GitHub issue #35.
        24961,
    }
)


def _is_auto_binary_sensor(point: dict, metadata: dict) -> bool:
    """Return True if this INPUT register point should be auto-classified as
    binary_sensor based on its firmware shape and point ID.

    Criteria (all must hold):
      - variableSize == 'u8'
      - minValue == 0, maxValue <= 1
      - no unit
      - not writable (an input register — see is_writable_point)
      - point ID not in _BINARY_SENSOR_EXCLUSIONS
      - if in VALUE_MAPPINGS['input'], its values must be exactly 0 and 1
      - description (if present) has at most 2 enum pairs
    """
    if (
        metadata.get("variableSize") != "u8"
        or metadata.get("minValue") != 0
        or
        # Any default > 1 (e.g. 100) is unobservable — only ever
        # compared with `> 1`. Verified empirically.
        metadata.get("maxValue", 99) > 1
        or metadata.get("unit")
        or is_writable_point(metadata)
    ):
        return False

    point_id = point.get("variableId")
    if point_id in _BINARY_SENSOR_EXCLUSIONS:
        return False

    # If the point has a VALUE_MAPPINGS entry, it is ground truth: only a
    # mapping of exactly the values 0 and 1 is binary. 3+ states is a
    # multi-state sensor, and so is a 2-state mapping on other values (e.g.
    # 2701 ACS status, 3 = Passive / 7 = Active) — published as a
    # binary_sensor, its first real value would only get it reclassified.
    # VALUE_MAPPINGS is a static dict literal that always has an 'input'
    # key, so .get('input', ...)'s default is dead code — verified
    # empirically, any default value (including None) is unobservable.
    if point_id is not None:
        mapping = VALUE_MAPPINGS.get("input", {}).get(point_id)
        if mapping is not None and set(mapping) != {0, 1}:
            return False

    # More than two described options is an enum, not a boolean. Counted by
    # parse_description_mapping, which reads both separators the firmware
    # uses ('=' and ':', mixed within one string on 22268) — counting only
    # '=' let a 0–0 register described '0: A, 1: B, 2: C' pass as binary.
    description = point.get("description", "")
    return len(parse_description_mapping(description or "") or {}) <= 2


def detect_entity_type(point: dict) -> tuple[str, str]:
    """Determine the HA entity type and category for a Nibe data point.

    Returns a (entity_type, entity_category) tuple.

    Resolution order:
      1. ENTITY_TYPE_OVERRIDES — explicit manual assignments always win.
      2. detect_holding_entity() — for MODBUS_HOLDING_REGISTER points.
      3. detect_input_entity()   — for MODBUS_INPUT_REGISTER points.
      4. ("sensor", "diagnostic") — safe fallback for unknown register types.
    """
    metadata = point.get("metadata", {})
    point_id = point["variableId"]
    # Same "only checked via `in` against known substrings" equivalence as
    # get_register_type — any non-matching default is unobservable.
    # Verified empirically.
    modbus_type = metadata.get("modbusRegisterType", "")

    if point_id in SAFETY_READ_ONLY_POINTS:
        return "sensor", "diagnostic"

    if point_id in ENTITY_TYPE_OVERRIDES:
        to_type = ENTITY_TYPE_OVERRIDES[point_id]
        category = "config" if to_type in CONFIG_ENTITY_TYPES else "diagnostic"
        if point_id not in _entity_type_override_warnings_issued:
            auto_type = _detect_type_without_override(point, metadata, modbus_type)[0]
            title = point.get("title") or f"Point {point_id}"
            log_detection.debug(
                "Point %d (%s): entity type overridden \u2014 auto-detect would use %r, using %r instead.",
                point_id,
                title,
                auto_type,
                to_type,
            )  # pragma: no mutate
            if len(_entity_type_override_warnings_issued) < _ENTITY_TYPE_WARNING_CACHE_MAX:
                _entity_type_override_warnings_issued.add(point_id)
        return to_type, category

    return _detect_type_without_override(point, metadata, modbus_type)


def _detect_type_without_override(point: dict, metadata: dict, modbus_type: str) -> tuple[str, str]:
    """The non-override branch of detect_entity_type, factored out so the
    override branch can compute 'what would auto-detect have said' (for the
    warning log) using the exact same dispatch logic, instead of a second,
    independently-maintained copy of it that could silently drift out of
    sync — the same class of bug the resolve_unit() consolidation fixed
    for unit handling.
    """
    if modbus_type == "MODBUS_HOLDING_REGISTER":
        return _detect_holding_entity(point, metadata)
    if modbus_type == "MODBUS_INPUT_REGISTER":
        return _detect_input_entity(point, metadata)
    return "sensor", "diagnostic"


def _detect_holding_entity(point: dict, metadata: dict) -> tuple[str, str]:
    """Classify a MODBUS_HOLDING_REGISTER point as a config entity.

    Detection priority:
      1. Special variableType (time/date/string) → dedicated HA types.
         NOTE: floating-point and binary variableType are in the API spec but
         not supported by this bridge — Nibe firmware uses integerValue+divisor
         for all numeric registers and no text or float writes are known.
      2. Manual VALUE_MAPPINGS entry → select.
      3. Firmware description with enum syntax → select.
      4. Switch shape (u8, 0–1, no unit) → switch.
      5. Has a physical unit → number.
      6. Default → number.
    """
    point_id = point["variableId"]
    # description is only checked against '=' and ',' substrings below —
    # any default without both (e.g. 'XXXX') is unobservable.
    description = point.get("description", "")
    # var_type's default is only ever compared with `==` against known
    # non-empty literals ("time"/"date"/"string"/"floating-point"/"binary")
    # — any non-matching default (None, 'XXXX', etc.) is unobservable.
    var_type = metadata.get("variableType", "")
    # var_size is used only inside the already-pragma'd
    # "floating-point"/f4/f8 log-only block below (see pragma comment there)
    # — its own assignment is therefore equally unobservable regardless of
    # key, default, or value. Verified empirically.
    var_size = metadata.get("variableSize", "")

    # HA MQTT time/date entities require ISO strings; Nibe firmware
    # stores these as raw integers (seconds / packed date).  Map to
    # number so the values are usable in HA without format conversion.
    if var_type == "time":
        return "number", "config"
    if var_type == "date":
        return "number", "config"
    if var_type == "string":
        # The log message content below has no effect on the return value.
        # pragma: no mutate start
        log_detection.debug(
            "Point %d has variableType='string' — text entities are not supported "
            "by this bridge. The point will be exposed as a text entity but "
            "write commands will not reach the controller.",
            point_id,
        )
        # pragma: no mutate end
        return "text", "config"
    # The condition below is log-only — neither branch returns, so no
    # mutation of it is externally observable via the return value.
    # pragma: no mutate start
    if var_type == "floating-point" or var_size in ("f4", "f8"):
        log_detection.debug(
            "Point %d has variableType='floating-point' / variableSize='%s' — "
            "native float registers are not supported by this bridge. "
            "Nibe firmware uses integerValue + divisor for all numeric values; "
            "if this point reads as zero or garbage, please report it.",
            point_id,
            var_size,
        )
        # Fall through — treat as a number using the integer+divisor path.
        # If the firmware genuinely returns a float here the value will be wrong,
        # but this is better than crashing or silently dropping the point.
    if var_type == "binary":
        log_detection.debug(
            "Point %d has variableType='binary' — binary_sensor classification "
            "cannot be determined from metadata alone; defaulting to switch. "
            "Add to ENTITY_TYPE_OVERRIDES if the correct type is known.",
            point_id,
        )
    # pragma: no mutate end

    # VALUE_MAPPINGS is a static dict literal that always has a 'holding'
    # key, so this default is dead code — verified empirically.
    if point_id in VALUE_MAPPINGS.get("holding", {}):
        return "select", "config"

    # >=2 to match get_entity_options()'s own minimum — a single-pair
    # description would otherwise classify as "select" but then render
    # zero dropdown options (get_entity_options requires >=2 to return
    # anything), producing a genuinely empty select entity.
    if len(parse_description_mapping(description) or {}) >= 2:
        return "select", "config"

    if is_switch_candidate(metadata):
        return "switch", "config"

    log_detection.debug(
        "Holding register %d: no explicit mapping or switch shape, defaulting to number", point_id
    )  # pragma: no mutate
    return "number", "config"


def _detect_input_entity(point: dict, metadata: dict) -> tuple[str, str]:
    """Classify a MODBUS_INPUT_REGISTER point as a diagnostic entity.

    binary_sensor is auto-detected for u8 INPUT registers that look like
    on/off status flags — see _is_auto_binary_sensor() for the full criteria.
    Points that cannot be auto-detected must be listed in ENTITY_TYPE_OVERRIDES.

    NOTE: floating-point and text variableType are not supported — see
    _detect_holding_entity for the full explanation.
    """
    point_id = point.get("variableId")
    # description is only used in the pragma'd final branch below (see its
    # comment: all remaining branches return the identical result) — its
    # key/default is unobservable regardless of value. Verified empirically.
    description = point.get("description", "")
    # var_type's default is only ever compared with `==` against known
    # non-empty literals — any non-matching default is unobservable.
    var_type = metadata.get("variableType", "")
    # var_size is used only inside the already-pragma'd float-check log-only
    # block below — its own assignment is equally unobservable.
    # Verified empirically.
    var_size = metadata.get("variableSize", "")

    # Read-only time/date registers — expose as sensor (raw integer).
    if var_type == "time":
        return "sensor", "diagnostic"
    if var_type == "date":
        return "sensor", "diagnostic"
    if var_type == "string":
        # The log message content below has no effect on the return value.
        # pragma: no mutate start
        log_detection.debug(
            "Point %d has variableType='string' (input register) — "
            "Nibe firmware does not use text registers in practice. "
            "Exposing as a read-only sensor showing the raw integer value.",
            point_id,
        )
        # pragma: no mutate end
        return "sensor", "diagnostic"
    # The condition below is log-only — no branch returns, so no mutation
    # of it or the log content is externally observable via the return value.
    # pragma: no mutate start
    if var_type == "floating-point" or var_size in ("f4", "f8"):
        log_detection.debug(
            "Point %d has variableType='floating-point' / variableSize='%s' "
            "(input register) — native float registers are not supported. "
            "Reading as integer+divisor; value may be incorrect.",
            point_id,
            var_size,
        )
        # Fall through to normal sensor path.
    # pragma: no mutate end

    if is_number_candidate(metadata):
        return "sensor", "diagnostic"

    # Auto-detect binary_sensor before VALUE_MAPPINGS and description checks —
    # a 2-state VALUE_MAPPINGS entry or a 2-pair description is still binary.
    if _is_auto_binary_sensor(point, metadata):
        log_detection.debug(
            "Input register %d: auto-classified as binary_sensor (u8, 0-1, no unit)", point_id
        )  # pragma: no mutate
        return "binary_sensor", "diagnostic"

    # These last two branches, and the function's final fallback below, all
    # return the identical ("sensor", "diagnostic") — so no test can ever
    # distinguish "branch taken" from "fell through to the default" by
    # checking the return value. Mutations here are structurally equivalent.
    if point.get("variableId") in VALUE_MAPPINGS.get("input", {}):  # pragma: no mutate
        return "sensor", "diagnostic"

    if "=" in description and "," in description:  # pragma: no mutate
        return "sensor", "diagnostic"

    log_detection.debug(
        "Input register %d: classified as sensor (no further classification)", point_id
    )  # pragma: no mutate
    return "sensor", "diagnostic"


# ============================================================================
# DEVICE CLASS MAPPING
# ============================================================================


def map_device_class(
    entity_type: str,
    unit: str,
    title: str,
) -> str | None:
    """Return the most appropriate HA device_class for an entity, or None.

    Only sensor and binary_sensor support device_class (number always returns
    None by policy — HA unit validation is strict and assignment without a
    matching unit causes errors).

    Resolution for sensor:
      Pass 1 — unambiguous unit lookup via _UNIT_TO_DEVICE_CLASS.
      Pass 2 — keyword scan via _SENSOR_KEYWORD_RULES.
      When both resolve, the unit wins (ground truth for physical dimension).
    """
    # Mutating "binary_sensor"/"number" here is unobservable: either string
    # still falls through into the identical early-return at the very next
    # check below, so the caller sees None either way. Verified empirically.
    if entity_type not in ("sensor", "binary_sensor", "number"):
        return None

    # binary_sensor: no keyword rules — device class assigned via overrides
    # by point ID in the publisher layer, not here.
    if entity_type in ("binary_sensor", "number"):
        return None

    unit_clean = clean_unit(unit)
    # A non-'' falsy-title default is unobservable given the current
    # _SENSOR_KEYWORD_RULES contain no keyword substring of it (e.g. no
    # keyword contains 'x') — verified empirically. Fragile if a future
    # keyword rule changes; re-verify then.
    title_lower = (title or "").lower()

    # `and`->`or` here is unobservable: _UNCLASSIFIABLE_UNITS and
    # _UNIT_TO_DEVICE_CLASS's keys never overlap, so
    # _UNIT_TO_DEVICE_CLASS.get(unit_clean) returns None for any
    # unclassifiable unit regardless of whether this guard let it through.
    # Verified empirically (confirmed the key sets are disjoint).
    unit_class: str | None = None
    if unit_clean and unit_clean not in _UNCLASSIFIABLE_UNITS:
        unit_class = _UNIT_TO_DEVICE_CLASS.get(unit_clean)

    # unit_class/keyword_class default to None but are only ever checked via
    # `if unit_class:`/`if keyword_class:` — None vs '' are both falsy and
    # otherwise only ever assigned a real (non-'') class string. Verified
    # empirically.
    keyword_class: str | None = None
    for keywords, device_class in _SENSOR_KEYWORD_RULES:
        if any(kw in title_lower for kw in keywords):
            keyword_class = device_class
            break

    if unit_class:
        return unit_class  # unit is ground truth; wins over keyword
    if keyword_class:
        _UNITLESS_CLASSES = {"aqi", "date", "enum", "ph", "timestamp"}  # pragma: no mutate
        if not unit_clean and keyword_class not in _UNITLESS_CLASSES:
            return None  # class mandates a unit but none is present
        return keyword_class

    return None
