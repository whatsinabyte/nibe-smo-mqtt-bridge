"""
nibe_discovery_config.py
=========================
Pure builders for HA MQTT discovery config payloads.

Each ``build_*_config`` function mutates a partially-built discovery config
dict in place, adding the fields specific to one HA entity type (state/command
topics, options, ranges, device class, ...). Topic strings are passed in by
the caller rather than constructed here, so this module never needs to know
about MQTT topic layout.

Responsibilities
-----------------
- Building the entity-type-specific portion of an HA discovery config dict.

What this module does NOT do
------------------------------
- No MQTT publishing, no HTTP calls, no file I/O.
- No MQTT topic string construction (callers pass in already-built topics).
"""

import logging

from nibe_entity_detection import (
    DEVICE_CLASS_OVERRIDES,
    get_entity_options,
    map_device_class,
)

log_entities = logging.getLogger("nibe.entities")

#: HA device classes that accumulate over time (total_increasing state class).
_ACCUMULATING_CLASSES: frozenset[str] = frozenset({"energy", "gas", "water", "volume"})  # pragma: no mutate

#: Point ID for the date sensor (days since 2010-01-01 → ISO date string).
_DATE_SENSOR_POINT_ID = 2685


def build_button_config(config: dict, command_topic: str) -> None:
    config["command_topic"] = command_topic


def build_switch_config(config: dict, state_topic: str, command_topic: str) -> None:
    config["state_topic"]   = state_topic
    config["command_topic"] = command_topic
    config["payload_on"]    = "1"
    config["payload_off"]   = "0"
    config["optimistic"]    = False


def build_number_config(
    config: dict,
    state_topic: str,
    command_topic: str,
    point_id: int,
    title: str,
    unit: str,
    metadata: dict,
    bulk_data: dict,
    range_warnings_issued: set[int],
) -> None:
    config["state_topic"]   = state_topic
    config["command_topic"] = command_topic
    config["optimistic"]    = False

    min_val     = metadata.get('minValue')
    max_val     = metadata.get('maxValue')
    divisor     = metadata.get('divisor', 1) or 1
    cached      = bulk_data.get(point_id, {})
    current_raw = cached.get('raw_value')

    if min_val is not None and max_val is not None:
        unit_str = f" {unit}" if unit else ""

        if min_val == max_val:
            # Degenerate range: firmware reports min==max for this register.
            # This is detected fresh from API metadata on every entity publish
            # (including after restart) so it cannot become stale even if a
            # firmware update changes the range.  The flag bypasses write-side
            # range enforcement, which is correct: we cannot know the valid
            # range so we pass the value through and let the controller decide.
            if point_id not in range_warnings_issued:
                log_entities.warning(
                    "Point %d (%s): degenerate range %g–%g (min==max) "
                    "— write-side range checks bypassed.",
                    point_id, title, min_val, max_val,
                )  # pragma: no mutate
                range_warnings_issued.add(point_id)
            if current_raw is not None:
                anchor       = current_raw / divisor
                fallback_min = min(anchor, -100)
                fallback_max = max(anchor,  100)
            else:
                fallback_min = -32768 / divisor
                fallback_max =  32767 / divisor
            config["min"]              = fallback_min
            config["max"]              = fallback_max
            config["_degenerate_range"] = True
        else:
            config["min"] = min_val / divisor
            config["max"] = max_val / divisor
            if (current_raw is not None
                    and point_id not in range_warnings_issued
                    and (current_raw < min_val or current_raw > max_val)):
                log_entities.warning(
                    "Point %d (%s): current value %g%s outside firmware range "
                    "%g–%g%s — writes restricted to firmware range.",
                    point_id, title,
                    current_raw / divisor, unit_str,
                    min_val / divisor, max_val / divisor, unit_str,
                )  # pragma: no mutate
                range_warnings_issued.add(point_id)
    if unit:
        config["unit_of_measurement"] = unit
    # step is the minimum increment HA allows in the number input widget.
    # It must be expressed in display units (post-divisor), so step = 1/divisor.
    # divisor=1  → step=1   (integer register: only whole numbers valid)
    # divisor=10 → step=0.1 (one decimal place register)
    # divisor=100→ step=0.01 (two decimal places)
    # Using round() with 10 decimal places avoids float representation noise
    # (e.g. 1/10 = 0.1000000000000000055… → round to 0.1).
    config["step"] = round(1 / divisor, 10)
    config["mode"] = "box"


def build_select_config(
    config: dict,
    state_topic: str,
    command_topic: str,
    point_id: int,
    metadata: dict,
    description: str,
) -> None:
    config["state_topic"]   = state_topic
    config["command_topic"] = command_topic
    config["optimistic"]    = False
    options = get_entity_options(point_id, metadata, description)
    if options:
        config["options"] = options


def build_binary_sensor_config(config: dict, state_topic: str, title: str) -> None:
    config["state_topic"] = state_topic
    config["payload_on"]  = "ON"
    config["payload_off"] = "OFF"
    device_class = map_device_class("binary_sensor", "", title)
    if device_class:
        config["device_class"] = device_class


def build_sensor_config(
    config: dict,
    state_topic: str,
    point_id: int,
    unit: str,
    title: str,
    metadata: dict,
) -> None:
    config["state_topic"] = state_topic
    # Special case: point 2685 is a date sensor (days since 2010-01-01
    # converted to ISO date string). Set device_class and return early.
    if point_id == _DATE_SENSOR_POINT_ID:
        config["device_class"] = "date"
        return
    if unit:
        config["unit_of_measurement"] = unit

    device_class = DEVICE_CLASS_OVERRIDES.get(
        point_id, map_device_class("sensor", unit, title)
    )
    is_instant = (
        point_id not in DEVICE_CLASS_OVERRIDES
        and unit == "kWh"
        and metadata.get('divisor') == 100
        and metadata.get('maxValue') == 0
        # ⚠ Heuristic: maxValue==0 is used as a proxy for "instantaneous power
        # reading" (e.g. compressor input power) rather than a lifetime energy
        # accumulator.  This works for the known Nibe register set but may
        # misclassify future firmware registers that genuinely have a zero max.
        # If a kWh sensor is wrongly treated as instantaneous, add its point_id
        # to DEVICE_CLASS_OVERRIDES in nibe_entity_detection.py to override.
    )
    has_numeric_value = bool(unit)

    if device_class in _ACCUMULATING_CLASSES and not is_instant:
        config["device_class"] = device_class
        config["state_class"]  = "total_increasing"
    elif device_class in _ACCUMULATING_CLASSES and is_instant:
        config["state_class"] = "measurement"
    elif device_class:
        config["device_class"] = device_class
        config["state_class"]  = "measurement"
    elif has_numeric_value:
        config["state_class"] = "measurement"

    # suggested_display_precision must ONLY be set for genuinely numeric
    # sensors. HA treats its mere presence as a declaration that the
    # entity is numeric, regardless of the value — setting it on a
    # string/enum status sensor (e.g. "Running", "Opening", "0.0.61")
    # causes HA to reject every state update with a ValueError, since
    # the state is text but the sensor now claims to be numeric.
    if has_numeric_value:
        decimal = metadata.get('decimal', 0)
        if decimal is not None:
            config["suggested_display_precision"] = int(decimal)
