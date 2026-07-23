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
get_entity_options(point_id, metadata, description) → list[str]
get_register_type(point)           → "input" | "holding" | None
clean_string(text)                 → str
apply_divisor(raw, divisor)        → str
reverse_divisor(display, divisor)  → int
create_entity_id(point_id)        → str
parse_description_mapping(desc)   → dict | None
"""


import logging
import math

log_detection = logging.getLogger("nibe.detection")


# ============================================================================
# ENTITY MODES
# ============================================================================
#
# Named groups of point IDs. "all" uses None as sentinel (replaced at runtime
# with the full discovered point list). "none" is an empty frozenset.

ESSENTIAL_POINTS = frozenset({
    4, 57, 599, 781, 832, 834, 993, 994, 997, 2491, 2494, 2495,
    2496, 2497, 2505, 2766, 2767, 2792, 3095, 3096, 3098, 3170,
    3667, 3671, 4603, 4604, 5025, 5033, 5034, 5035, 5036,
})

UPLINK_POINTS = ESSENTIAL_POINTS | frozenset({
    1758, 1708, 121, 2695, 54, 4651, 4821, 1838,
    2471, 2472, 2688, 992, 835, 836, 837, 838, 840,
    841, 22268, 842, 843, 845, 3097, 3353, 2453,
    14987, 2471, 2472, 2509, 2527, 1766, 4084, 3825, 6138, 6139, 2506,
})

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
    "essential":   ESSENTIAL_POINTS,
    "monitoring":  UPLINK_POINTS,
    "advanced": ADVANCED_POINTS,
    "menus":    MENU_POINTS,    # also gates Lovelace menu dashboard provisioning
    "all":      None,           # sentinel — replaced with full point list at runtime
    "none":     frozenset(),
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

        # PV panels — verified empirically, no firmware description
        1021: {10: "PV surplus active", 40: "Normal operation"},

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
    },
    "holding": {
        # Language selection — ordering reflects Nibe's market priority
        3745: {
             0: "English",
             1: "Svenska",
             2: "Deutsch",
             3: "Français",
             4: "Español",
             5: "Suomi",
             6: "Lietuvių",
             7: "Čeština",
             8: "Polski",
             9: "Nederlands",
            10: "Norsk",
            11: "Dansk",
            12: "Eesti",
            13: "Latviešu",
            14: "Русский",
            15: "Italiano",
            16: "Magyar",
            17: "Slovenčina",
            18: "Türkçe",
            19: "Hrvatski",
            20: "Română",
            21: "Íslenska",
            22: "Slovenščina",
            23: "Українська",
            24: "Български",
        },
        3976: {0: "Own setting", 1: "Radiator", 2: "Underfloor heating",
               3: "Radiator & Underfloor heating"},
        3751: {0: "Auto", 1: "Manual", 2: "Additional heat only"},
        4651: {0: "Intermittent", 1: "Auto"},
        4821: {0: "Intermittent", 1: "Auto"},
        4729: {0: "Intermittent", 1: "Auto"},
        4778: {0: "Intermittent", 1: "Auto"},
        # Smart Price Adaption status and settings
        3292: {0: "Normal", 1: "Low price", 2: "Overcapacity", 3: "Blocking"},
        # Operating prioritisation
        56150: {10: "Off", 20: "Hot water", 30: "Heating", 40: "Pool", 60: "Cooling"},
        # Heat pump type codes (best-effort, not officially documented)
        2471: {17: "S2125-12", 16: "S2125-8", 18: "S2125-16",
               30: "F2040-6", 31: "F2040-8", 32: "F2040-12", 33: "F2040-16",
               40: "SMO S40"},
        2527: {17: "S2125-12", 16: "S2125-8", 18: "S2125-16",
               30: "F2040-6", 31: "F2040-8", 32: "F2040-12", 33: "F2040-16",
               40: "SMO S40"},
        4790: {0: "Off", 1: "Comfort", 2: "Saving", 3: "Saving PLUS"},
        # Smart Energy Source control method
        5269: {0: "Price per kWh", 1: "CO2"},
        # Time format
        3933: {0: "12h", 1: "24h"},
    }
}

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
    3478:  'button',   # Reset alarm — trigger-only, not a number
    8052:  'button',   # Start fan de-icing — trigger-only, not a number
    12392: 'switch',   # Show outdoor temperature — 0/1 but auto-detects as number
    12393: 'switch',   # Show indoor temperature — 0/1 but auto-detects as number
    3706:  'switch',   # Periodic increase activated — persistent on/off, not a number
    4970:  'number',   # blockFreq 2 — auto-detects as switch (0/1 shape) but is a frequency value
    4969:  'number',   # blockFreq 1 — same
    8982:  'switch',   # Away mode — max=0 in metadata (firmware quirk) so auto-detects as number
    3754:  'switch',   # Activate forced control — same firmware quirk
    # ── MODBUS_NO_REGISTER — would fall through to sensor/diagnostic without override ──
    32824: 'switch',   # Power limitation activation
    # ── THS-10 accessory — auto-detects as number without override ────────────
    5110:  'switch',   # Prevent condensation climate system 1
    5214:  'switch',   # Limit humidity in the room, cooling climate system 1
    # ── MODBUS_HOLDING_REGISTER + isWritable=False — handled by auto-detect ──
    # _detect_holding_entity now returns ('sensor', 'diagnostic') for all
    # HOLDING registers where isWritable=False. The overrides below are no
    # longer needed — kept as comments for historical reference.
    # 3937: 'sensor'   # Auxiliary operation on alarm
    # 4030: 'sensor'   # More hot water (Number of minutes)
    # 5222: 'sensor'   # Delay timer EME
    # 1948: 'sensor'   # Holiday function status
    # ── Time-of-day registers — stored as seconds, shown as HH:MM ────────────
    3708:  'time',     # Periodic increase start time
    12401: 'time',     # HW circulation start time period 1
    12402: 'time',     # HW circulation start time period 2
    12403: 'time',     # HW circulation stop time period 1
    # ── Circulation pump (EB101) — auto-detects as switch (0/1 shape) ─────────
    4562:  'switch',   # Manual heating medium pump speed (0=auto, 1=manual)
    # ── Binary sensors — values from firmware description ─────────────────────
    # ── binary_sensor — non-INPUT or non-standard shape, cannot be auto-detected ──
    # Point 22077 is s16 + isWritable=True so _is_auto_binary_sensor() skips it.
    22077: 'binary_sensor',  # AUX from Modbus
}

# Entity types that belong in the HA "config" category (writable controls).
CONFIG_ENTITY_TYPES: list[str] = [
    'switch', 'number', 'select', 'button', 'text', 'time'
]

# Per-point unit overrides.
# Key: point_id  Value: replacement unit string sent to HA.
UNIT_OVERRIDES: dict[int, str] = {
    25165: "kW",
    25166: "kW",
    4562:  "",     # switch (0=auto, 1=manual) — firmware wrongly reports unit='%'
    50825: "%",    # THS-10 accessory point — firmware reports no unit but value is %
    50827: "%",    # Humidity: ths-10 — firmware unit is "%RH", which HA's auto-detection rejects
    # variableType=date registers — point 2685 ("Date, periodic hot water")
    # is the only known instance.  The raw integerValue (e.g. 5265) is
    # suspected to be days since 2010-01-01 (5265 → 2024-06-01 is consistent
    # with one live installation), but this is unverified.  Exposed as a plain
    # sensor showing the raw integer until the encoding is confirmed.
    # variableType=time registers — formerly exposed as number with "s" unit.
    # Now exposed as HA time entities (HH:MM:SS), no unit needed.
    # Unit overrides below are intentionally removed; see ENTITY_TYPE_OVERRIDES.
    822: "s",  823: "s",  824: "s",  825: "s",
    1205: "DM", 1206: "DM", 1207: "DM", 1208: "DM",
    1209: "DM", 1210: "DM", 1211: "DM", 1212: "DM",
    1213: "DM", 1214: "DM", 1215: "DM", 1216: "DM",
    1217: "DM", 1218: "DM", 1219: "DM",
}

# Per-point device_class overrides.
DEVICE_CLASS_OVERRIDES: dict[int, str] = {
    25165: "power",
    25166: "power",
}

# Unit → HA device_class lookup (after _UNIT_NORMALISE has been applied).
_UNIT_TO_DEVICE_CLASS: dict[str, str] = {
    "°C": "temperature", "°F": "temperature", "K": "temperature",
    "Wh": "energy",  "kWh": "energy",  "MWh": "energy",
    "GWh": "energy", "TWh": "energy",
    "mW": "power",   "W": "power",     "kW": "power",
    "MW": "power",   "GW": "power",    "TW": "power",
    "mVA": "apparent_power", "VA": "apparent_power", "kVA": "apparent_power",
    "mvar": "reactive_power", "var": "reactive_power", "kvar": "reactive_power",
    "mA": "current", "A": "current",
    "µV": "voltage", "mV": "voltage",  "V": "voltage",
    "kV": "voltage", "MV": "voltage",
    "Hz": "frequency", "kHz": "frequency", "MHz": "frequency", "GHz": "frequency",
    "cbar": "pressure", "mbar": "pressure", "bar": "pressure",
    "Pa": "pressure",   "hPa": "pressure",  "kPa": "pressure",
    "mPa": "pressure",  "mmHg": "pressure", "inHg": "pressure", "psi": "pressure",
    "%RH": "humidity",
    "mL/s": "volume_flow_rate", "L/s": "volume_flow_rate",
    "L/min": "volume_flow_rate", "L/h": "volume_flow_rate",
    "l/h": "volume_flow_rate",
    "m³/s": "volume_flow_rate", "m³/min": "volume_flow_rate",
    "m³/h": "volume_flow_rate",
    "s": "duration",   "ms": "duration",  "µs": "duration",
    "min": "duration", "h": "duration",
    "m/s": "wind_speed", "km/h": "wind_speed", "mph": "wind_speed", "kn": "wind_speed",
}

# Firmware unit variants → canonical HA unit strings.
_UNIT_NORMALISE: dict[str, str] = {
    "days": "d",
    # Firmware mojibake: Latin-1 '°' mis-decoded as UTF-8 produces 'Â°'
    "Â°C": "°C",
    "Â°F": "°F",
    "Â°":  "°",
}

# Unit strings for which HA has no valid device_class.
_UNCLASSIFIABLE_UNITS: frozenset[str] = frozenset({"%", "DM", "rpm"})

# Title keyword → device_class rules (language-agnostic hardware codes only).
_SENSOR_KEYWORD_RULES: list[tuple] = [
    (("bt1",  "bt2",  "bt3",  "bt4",  "bt6",  "bt7",
      "bt10", "bt11", "bt12", "bt14", "bt16", "bt17",
      "bt20", "bt21", "bt22", "bt23", "bt24", "bt25",
      "bt26", "bt27", "bt28", "bt29",
      "bt50", "bt51", "bt57", "bt64", "bt65",
      "bt70", "bt71", "bt75", "bt76", "bt77",
      "bt81", "bt82", "bt83", "bt84"), "temperature"),
    (("bp1", "bp2", "bp3", "bp4", "bp5",
      "bp6", "bp7", "bp8", "bp9", "bp10", "bp11"), "pressure"),
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

def clean_string(text) -> str | None:
    """Normalise a Nibe API string for use as an HA entity title.

    Strips stray quotes, non-breaking spaces, and UTF-8 mojibake bytes that
    appear in some firmware versions, then collapses internal whitespace.
    """
    if not text or not isinstance(text, str):
        return text
    cleaned = text.strip().strip('"').strip("'")
    cleaned = cleaned.replace('\u00c2', '').replace('\u00a0', ' ').replace('\xa0', ' ')  # pragma: no mutate
    # U+00AD is the soft-hyphen inserted by Nibe firmware as an invisible
    # line-break hint.  It does not display but breaks substring search:
    # "exter\u00adn" does not match "extern".  Strip it entirely.  # pragma: no mutate
    cleaned = cleaned.replace('\u00ad', '')  # pragma: no mutate
    return ' '.join(cleaned.split())


def clean_unit(unit) -> str:
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
        return ''
    cleaned = unit.strip().replace('\u00c2', '').replace('\u00a0', ' ').replace('\xa0', ' ')  # pragma: no mutate
    cleaned = ' '.join(cleaned.split())
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
    effective = divisor if divisor and divisor != 0 else 1
    if effective == 1:
        return str(raw_value)
    decimal_places = max(0, math.ceil(math.log10(effective)))
    return f"{raw_value / effective:.{decimal_places}f}".rstrip('0').rstrip('.')


def reverse_divisor(display_value: float, divisor: int) -> int:
    """Convert a user-supplied display value back to a raw Nibe integer.

    HA number entities send the human-readable (post-divisor) value.
    Multiply back by divisor and round to int before writing to the API.
    ``divisor=0`` is treated as ``divisor=1`` — see ``apply_divisor`` for
    the full rationale.
    """
    effective = divisor if divisor and divisor != 0 else 1
    return int(round(display_value * effective))


def create_entity_id(point_id: int) -> str:
    """Return the stable MQTT entity ID for a Nibe point.

    Uses only the numeric point ID so topic paths are language-agnostic
    and permanent regardless of firmware title changes.
    """
    return f"nibe_{point_id}"


def get_register_type(point: dict) -> str | None:
    """Return 'input', 'holding', or None based on the point's Modbus register type."""
    modbus_type = point.get('metadata', {}).get('modbusRegisterType', '')
    if 'INPUT' in modbus_type:
        return "input"
    if 'HOLDING' in modbus_type:
        return "holding"
    return None


# ============================================================================
# DESCRIPTION / VALUE MAPPING
# ============================================================================

def parse_description_mapping(description: str) -> dict | None:
    """Parse a Nibe firmware enum description string into a {int: str} mapping.

    Handles both orderings used by different register families:
      "0 = Off, 1 = Active"   (integer on the left)
      "Auto = 0, Manual = 1"  (label on the left)

    Returns None if no parseable key=value pairs are found.
    Results are cached — firmware descriptions are static for the bridge's lifetime.
    """
    if not description or '=' not in description:
        return None

    if description in _description_mapping_cache:
        return _description_mapping_cache[description]

    mapping = {}
    for part in description.split(','):
        part = part.strip()
        if '=' not in part:
            continue
        left, right = part.split('=', 1)
        left, right = left.strip(), right.strip()
        try:
            mapping[int(left)] = right
        except ValueError:
            try:
                mapping[int(right)] = left
            except ValueError:
                continue

    result = mapping if mapping else None
    if len(_description_mapping_cache) < _DESCRIPTION_CACHE_MAX:
        _description_mapping_cache[description] = result
    return result


def get_value_mapping(
    point_id: int,
    point_data: dict,
    register_type: str | None = None,
) -> dict | None:
    """Return a {raw_int: label_str} mapping for a point, or None.

    Lookup order:
      1. VALUE_MAPPINGS[register_type][point_id] — manual table (takes precedence).
      2. parse_description_mapping(point_data['description']) — firmware-provided enum.
    """
    if register_type:
        manual = VALUE_MAPPINGS.get(register_type, {}).get(point_id)
        if manual:
            return manual

    description = point_data.get('description', '')
    return parse_description_mapping(description)


def get_entity_options(
    point_id: int,
    metadata: dict,
    description: str,
) -> list[str]:
    """Return the ordered list of option labels for a select entity.

    Returns an empty list when no options can be determined.
    """
    register_type = (
        "holding" if 'HOLDING' in metadata.get('modbusRegisterType', '') else "input"
    )
    mapping = VALUE_MAPPINGS.get(register_type, {}).get(point_id)

    if mapping:
        return [text for _, text in sorted(mapping.items())]

    if '=' in description and ',' in description:
        options = []
        for part in description.split(','):
            part = part.strip()
            if '=' not in part:
                continue
            left, right = part.split('=', 1)
            left, right = left.strip(), right.strip()
            try:
                int(left)
                text = right
            except ValueError:
                text = left
            if text and text not in options:
                options.append(text)
        if len(options) >= 2:
            return options

    return []


# ============================================================================
# ENTITY TYPE DETECTION
# ============================================================================

def is_switch_candidate(metadata: dict) -> bool:
    """Return True if a holding register has the signature of a boolean on/off switch."""
    return all([
        metadata.get('modbusRegisterType') == "MODBUS_HOLDING_REGISTER",
        metadata.get('unit', '') == '',
        metadata.get('variableSize') == "u8",
        metadata.get('minValue', 0) == 0,
        metadata.get('maxValue', 0) == 1,
        metadata.get('divisor', 1) == 1,
    ])


def is_number_candidate(metadata: dict) -> bool:
    """Return True if the register has a physical unit, implying a numeric measurement."""
    return bool(metadata.get('unit', '').strip())



# ── Auto-detection support for binary_sensor (INPUT registers only) ───────────
#
# INPUT register u8 points with min=0, max≤1, no unit, and isWritable=False are
# almost always on/off status flags. We auto-detect them as binary_sensor rather
# than requiring every flag to be listed in ENTITY_TYPE_OVERRIDES.
#
# Points that match the shape but are NOT binary sensors are listed explicitly
# in _BINARY_SENSOR_EXCLUSIONS by point ID. This is unambiguous and immune to
# soft-hyphen or title-translation quirks.

_BINARY_SENSOR_EXCLUSIONS: frozenset[int] = frozenset({
    # Fan / pump speed registers — numeric RPM values, not flags
    765, 766, 1495, 3354, 3357, 3360,
    # Time-count registers
    818, 819, 820, 821, 822, 823, 824, 825,
    # Numeric codes and indices
    832,   # Alarm number from outdoor air heat pump (EB101)
    1511,  # Serial index (EB101)
    # Equipment type / size — numeric enum values
    2471, 2472,   # Heat pump type + compressor size (EB101)
    2527, 2528,   # Heat pump type + compressor size (EB100)
    # Step count
    6717,         # Ext. add. heat active steps
    # Firmware version register (bitfield-encoded, not a flag)
    14987,        # Version, inverter (EB101)
    # Compressor count registers — value is 0..N, not a binary flag
    666, 667, 668, 669, 670,        # Available compressors (heating/HW/pool/cooling)
    1999, 2000, 2001, 2059, 2882,   # Docked compressors
    2706, 2707, 2708, 2709, 2729,   # Used compressors
    # Multi-state registers that happen to share the binary shape
    1758, 55000,  # Priority (5-state)
    55335,        # EEV Control Mode (EB101) — multi-state valve control
})


def _is_auto_binary_sensor(point: dict, metadata: dict) -> bool:
    """Return True if this INPUT register point should be auto-classified as
    binary_sensor based on its firmware shape and point ID.

    Criteria (all must hold):
      - variableSize == 'u8'
      - minValue == 0, maxValue <= 1
      - no unit
      - isWritable is False
      - point ID not in _BINARY_SENSOR_EXCLUSIONS
      - if in VALUE_MAPPINGS['input'], must have exactly 2 states (not 3+)
      - description (if present) has at most 2 enum pairs
    """
    if (metadata.get('variableSize') != 'u8' or
            metadata.get('minValue') != 0 or
            metadata.get('maxValue', 99) > 1 or
            metadata.get('unit') or
            metadata.get('isWritable') is not False):
        return False

    point_id = point.get('variableId')
    if point_id in _BINARY_SENSOR_EXCLUSIONS:
        return False

    # If the point has a VALUE_MAPPINGS entry, use the state count as ground
    # truth — 2 states is binary, 3+ states is a multi-state sensor.
    if point_id is not None:
        mapping = VALUE_MAPPINGS.get('input', {}).get(point_id)
        if mapping is not None and len(mapping) > 2:
            return False

    description = point.get('description', '')
    if description:
        pairs = [p for p in description.split(',') if '=' in p]
        if len(pairs) > 2:
            return False

    return True


def detect_entity_type(point: dict):
    """Determine the HA entity type and category for a Nibe data point.

    Returns a (entity_type, entity_category) tuple.

    Resolution order:
      1. ENTITY_TYPE_OVERRIDES — explicit manual assignments always win.
      2. detect_holding_entity() — for MODBUS_HOLDING_REGISTER points.
      3. detect_input_entity()   — for MODBUS_INPUT_REGISTER points.
      4. ("sensor", "diagnostic") — safe fallback for unknown register types.
    """
    metadata = point.get('metadata', {})
    point_id = point['variableId']
    modbus_type = metadata.get('modbusRegisterType', '')

    if point_id in ENTITY_TYPE_OVERRIDES:
        to_type  = ENTITY_TYPE_OVERRIDES[point_id]
        category = "config" if to_type in CONFIG_ENTITY_TYPES else "diagnostic"
        if point_id not in _entity_type_override_warnings_issued:
            auto_type = _detect_type_without_override(point, metadata, modbus_type)[0]
            title = point.get('title') or f"Point {point_id}"
            log_detection.debug(
                "Point %d (%s): entity type overridden \u2014 auto-detect would use %r, using %r instead.",
                point_id, title, auto_type, to_type,
            )  # pragma: no mutate
            if len(_entity_type_override_warnings_issued) < _ENTITY_TYPE_WARNING_CACHE_MAX:
                _entity_type_override_warnings_issued.add(point_id)
        return to_type, category

    return _detect_type_without_override(point, metadata, modbus_type)


def _detect_type_without_override(point: dict, metadata: dict, modbus_type: str):
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


def _detect_holding_entity(point: dict, metadata: dict):
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
    point_id    = point['variableId']
    description = point.get('description', '')
    var_type    = metadata.get('variableType', '')
    var_size    = metadata.get('variableSize', '')

    # HA MQTT time/date entities require ISO strings; Nibe firmware
    # stores these as raw integers (seconds / packed date).  Map to
    # number so the values are usable in HA without format conversion.
    if var_type == "time":   return "number", "config"  # noqa: E701
    if var_type == "date":   return "number", "config"  # noqa: E701
    if var_type == "string":
        log_detection.debug(
            "Point %d has variableType='string' — text entities are not supported "
            "by this bridge. The point will be exposed as a text entity but "
            "write commands will not reach the controller.",
            point_id,
        )  # pragma: no mutate
        return "text", "config"
    if var_type == "floating-point" or var_size in ("f4", "f8"):
        log_detection.debug(
            "Point %d has variableType='floating-point' / variableSize='%s' — "
            "native float registers are not supported by this bridge. "
            "Nibe firmware uses integerValue + divisor for all numeric values; "
            "if this point reads as zero or garbage, please report it.",
            point_id, var_size,
        )  # pragma: no mutate
        # Fall through — treat as a number using the integer+divisor path.
        # If the firmware genuinely returns a float here the value will be wrong,
        # but this is better than crashing or silently dropping the point.
    if var_type == "binary":
        log_detection.debug(
            "Point %d has variableType='binary' — binary_sensor classification "
            "cannot be determined from metadata alone; defaulting to switch. "
            "Add to ENTITY_TYPE_OVERRIDES if the correct type is known.",
            point_id,
        )  # pragma: no mutate

    if point_id in VALUE_MAPPINGS.get("holding", {}):
        return "select", "config"

    if '=' in description and ',' in description:
        return "select", "config"

    # isWritable=False on a HOLDING register means the REST API will reject any
    # write — the firmware marks these as Modbus-TCP-only. Expose as a read-only
    # sensor rather than a writable control (number/switch).
    if metadata.get('isWritable') is False:
        return "sensor", "diagnostic"

    if is_switch_candidate(metadata):
        return "switch", "config"

    log_detection.debug("Holding register %d: no explicit mapping or switch shape, defaulting to number", point_id)  # pragma: no mutate
    return "number", "config"


def _detect_input_entity(point: dict, metadata: dict):
    """Classify a MODBUS_INPUT_REGISTER point as a diagnostic entity.

    binary_sensor is auto-detected for u8 INPUT registers that look like
    on/off status flags — see _is_auto_binary_sensor() for the full criteria.
    Points that cannot be auto-detected must be listed in ENTITY_TYPE_OVERRIDES.

    NOTE: floating-point and text variableType are not supported — see
    _detect_holding_entity for the full explanation.
    """
    point_id    = point.get('variableId')
    description = point.get('description', '')
    var_type    = metadata.get('variableType', '')
    var_size    = metadata.get('variableSize', '')

    # Read-only time/date registers — expose as sensor (raw integer).
    if var_type == "time":   return "sensor",      "diagnostic"  # noqa: E701
    if var_type == "date":   return "sensor",      "diagnostic"  # noqa: E701
    if var_type == "string":
        log_detection.debug(
            "Point %d has variableType='string' (input register) — "
            "Nibe firmware does not use text registers in practice. "
            "Exposing as a read-only sensor showing the raw integer value.",
            point_id,
        )  # pragma: no mutate
        return "sensor", "diagnostic"
    if var_type == "floating-point" or var_size in ("f4", "f8"):
        log_detection.debug(
            "Point %d has variableType='floating-point' / variableSize='%s' "
            "(input register) — native float registers are not supported. "
            "Reading as integer+divisor; value may be incorrect.",
            point_id, var_size,
        )  # pragma: no mutate
        # Fall through to normal sensor path.

    if is_number_candidate(metadata):
        return "sensor", "diagnostic"

    # Auto-detect binary_sensor before VALUE_MAPPINGS and description checks —
    # a 2-state VALUE_MAPPINGS entry or a 2-pair description is still binary.
    if _is_auto_binary_sensor(point, metadata):
        log_detection.debug("Input register %d: auto-classified as binary_sensor (u8, 0-1, no unit)", point_id)  # pragma: no mutate
        return "binary_sensor", "diagnostic"

    if point.get('variableId') in VALUE_MAPPINGS.get("input", {}):
        return "sensor", "diagnostic"

    if '=' in description and ',' in description:
        return "sensor", "diagnostic"

    log_detection.debug("Input register %d: classified as sensor (no further classification)", point_id)  # pragma: no mutate
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
    if entity_type not in ("sensor", "binary_sensor", "number"):
        return None

    # binary_sensor: no keyword rules — device class assigned via overrides
    # by point ID in the publisher layer, not here.
    if entity_type in ("binary_sensor", "number"):
        return None

    unit_clean  = clean_unit(unit)
    title_lower = (title or "").lower()

    unit_class: str | None = None
    if unit_clean and unit_clean not in _UNCLASSIFIABLE_UNITS:
        unit_class = _UNIT_TO_DEVICE_CLASS.get(unit_clean)

    keyword_class: str | None = None
    for keywords, device_class in _SENSOR_KEYWORD_RULES:
        if any(kw in title_lower for kw in keywords):
            keyword_class = device_class
            break

    if unit_class:
        return unit_class   # unit is ground truth; wins over keyword
    if keyword_class:
        _UNITLESS_CLASSES = {"aqi", "date", "enum", "ph", "timestamp"}  # pragma: no mutate
        if not unit_clean and keyword_class not in _UNITLESS_CLASSES:
            return None     # class mandates a unit but none is present
        return keyword_class

    return None
