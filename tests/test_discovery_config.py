"""
test_discovery_config.py
=========================
Nibe_discovery_config tests.
Part of the Nibe S-Series MQTT Bridge test suite.
Shared fixtures are in conftest.py.

nibe_discovery_config.py is a pure module (no I/O, no MQTT) with no prior
dedicated test file — it was previously only covered indirectly through
test_mqtt_publisher.py / test_generate.py / test_lovelace.py call sites.
These tests exercise its build_*_config() functions directly.
"""

import unittest

from conftest import _nibe_divisor, _nibe_point_id
from hypothesis import given
from hypothesis import strategies as st


class TestBuildSelectConfig(unittest.TestCase):
    def test_sets_fixed_fields_and_no_options_for_unparseable_description(self):
        from nibe_discovery_config import build_select_config

        config = {}
        build_select_config(config, "state/t", "cmd/t", 999900, "no options here")
        self.assertEqual(config["state_topic"], "state/t")
        self.assertEqual(config["command_topic"], "cmd/t")
        self.assertIs(config["optimistic"], False)
        self.assertNotIn("options", config)


class TestBuildSwitchConfig(unittest.TestCase):
    def test_sets_fixed_fields(self):
        from nibe_discovery_config import build_switch_config

        config = {}
        build_switch_config(config, "state/t", "cmd/t")
        self.assertEqual(
            config,
            {
                "state_topic": "state/t",
                "command_topic": "cmd/t",
                "payload_on": "1",
                "payload_off": "0",
                "optimistic": False,
            },
        )


class TestBuildButtonConfig(unittest.TestCase):
    def test_sets_command_topic_only(self):
        from nibe_discovery_config import build_button_config

        config = {}
        build_button_config(config, "cmd/t")
        self.assertEqual(config, {"command_topic": "cmd/t"})


class TestBuildNumberConfigStep(unittest.TestCase):
    """step = 1/divisor is the HA number widget's minimum increment.

    A divisor/1 swap, or step computed from the wrong variable, would not
    be caught by an example test that happens to use divisor=10 (where
    1/10 and 10/1 both "look like" plausible steps) — the inverse
    relationship must hold for many divisor values, not just one."""

    @given(_nibe_divisor.filter(lambda d: d and d > 0))
    def test_step_times_divisor_is_approximately_one(self, divisor):
        from nibe_discovery_config import build_number_config

        config = {}
        metadata = {"divisor": divisor}
        build_number_config(
            config,
            "state/t",
            "cmd/t",
            1,
            "Title",
            "",
            metadata,
            {},
            set(),
        )
        self.assertAlmostEqual(config["step"] * divisor, 1, places=6)


class TestBuildNumberConfigDegenerateRange(unittest.TestCase):
    """When firmware reports minValue == maxValue (a degenerate range), the
    fallback bounds must always straddle the current value with at least
    ±100 of headroom: fallback_min <= -100 and fallback_max >= 100. A
    min()/max() swap in the fallback computation would silently produce a
    narrower-than-intended range without raising or crashing — only
    observable by checking the bound invariant itself, not a fixed
    example value."""

    @given(
        _nibe_point_id,
        st.integers(min_value=-1_000_000, max_value=1_000_000),
        _nibe_divisor.filter(lambda d: d and d > 0),
    )
    def test_fallback_bounds_always_straddle_the_anchor(
        self,
        point_id,
        current_raw,
        divisor,
    ):
        from nibe_discovery_config import build_number_config

        config = {}
        metadata = {"minValue": 5, "maxValue": 5, "divisor": divisor}
        bulk_data = {point_id: {"raw_value": current_raw}}
        build_number_config(
            config,
            "state/t",
            "cmd/t",
            point_id,
            "Title",
            "",
            metadata,
            bulk_data,
            set(),
        )
        self.assertTrue(config["_degenerate_range"])
        self.assertLessEqual(config["min"], -100)
        self.assertGreaterEqual(config["max"], 100)

    def test_fallback_without_current_value_uses_full_register_range(self):
        from nibe_discovery_config import build_number_config

        config = {}
        metadata = {"minValue": 5, "maxValue": 5, "divisor": 1}
        build_number_config(
            config,
            "state/t",
            "cmd/t",
            999,
            "Title",
            "",
            metadata,
            {},
            set(),
        )
        self.assertEqual(config["min"], -32768)
        self.assertEqual(config["max"], 32767)


class TestBuildNumberConfigNormalRange(unittest.TestCase):
    """A non-degenerate min/max must be scaled by the divisor exactly —
    config['min'] == minValue / divisor, not the raw firmware integer."""

    @given(
        st.integers(min_value=-100_000, max_value=100_000),
        st.integers(min_value=-100_000, max_value=100_000),
        _nibe_divisor.filter(lambda d: d and d > 0),
    )
    def test_min_and_max_are_scaled_by_divisor(self, min_val, max_val, divisor):
        from nibe_discovery_config import build_number_config

        if min_val == max_val:
            max_val += 1
        config = {}
        metadata = {"minValue": min_val, "maxValue": max_val, "divisor": divisor}
        build_number_config(
            config,
            "state/t",
            "cmd/t",
            1,
            "Title",
            "",
            metadata,
            {},
            set(),
        )
        self.assertEqual(config["min"], min_val / divisor)
        self.assertEqual(config["max"], max_val / divisor)
        self.assertNotIn("_degenerate_range", config)


class TestBuildSensorConfigDateSpecialCase(unittest.TestCase):
    def test_point_2685_is_a_date_sensor(self):
        from nibe_discovery_config import build_sensor_config

        config = {}
        build_sensor_config(config, "state/t", 2685, "", "Date", {})
        self.assertEqual(config["device_class"], "date")
        self.assertNotIn("state_class", config)


class TestBuildSensorConfigAccumulatingVsInstant(unittest.TestCase):
    """The accumulating/instant device_class branching is a 4-way
    if/elif over (device_class in _ACCUMULATING_CLASSES, is_instant).
    Each branch is pinned by a real example so a mutation to any one of
    the boolean conditions changes which branch fires."""

    def test_energy_accumulator_gets_total_increasing(self):
        from nibe_discovery_config import build_sensor_config

        config = {}
        metadata = {"divisor": 10, "maxValue": 60000}
        build_sensor_config(config, "state/t", 6139, "kWh", "Total energy", metadata)
        self.assertEqual(config["device_class"], "energy")
        self.assertEqual(config["state_class"], "total_increasing")

    def test_instantaneous_kwh_power_gets_measurement_only(self):
        """maxValue == 0 + divisor == 100 is the heuristic for an
        instantaneous reading rather than a lifetime accumulator — it
        must get 'measurement', not 'total_increasing', and must NOT
        carry a device_class (per the elif chain skipping that branch)."""
        from nibe_discovery_config import build_sensor_config

        config = {}
        metadata = {"divisor": 100, "maxValue": 0}
        build_sensor_config(config, "state/t", 999001, "kWh", "Compressor power", metadata)
        self.assertEqual(config["state_class"], "measurement")
        self.assertNotIn("device_class", config)

    def test_non_accumulating_device_class_gets_measurement(self):
        from nibe_discovery_config import build_sensor_config

        config = {}
        metadata = {"divisor": 10}
        build_sensor_config(config, "state/t", 999002, "°C", "Outdoor temperature", metadata)
        self.assertEqual(config["device_class"], "temperature")
        self.assertEqual(config["state_class"], "measurement")

    def test_no_device_class_but_numeric_unit_still_gets_measurement(self):
        """A unit that doesn't resolve to any known device_class (neither
        via _UNIT_TO_DEVICE_CLASS nor a title keyword) must still get
        state_class='measurement' from the has_numeric_value fallback."""
        from nibe_discovery_config import build_sensor_config

        config = {}
        metadata = {"divisor": 1}
        build_sensor_config(config, "state/t", 999003, "zzq", "Odd counter", metadata)
        self.assertEqual(config.get("state_class"), "measurement")
        self.assertNotIn("device_class", config)


class TestBuildSensorConfigDeviceClassOverride(unittest.TestCase):
    """DEVICE_CLASS_OVERRIDES.get(point_id, ...) must be keyed on the
    actual point_id — a mutation to the lookup key (e.g. None) would only
    be observable for a point_id genuinely present in that table, so an
    override-less example test can never catch it."""

    def test_overridden_point_id_uses_the_override_class(self):
        from nibe_discovery_config import build_sensor_config

        config = {}
        # Point 25165 is hardcoded in DEVICE_CLASS_OVERRIDES to 'power',
        # regardless of what map_device_class would otherwise compute for
        # this unit/title combination.
        build_sensor_config(config, "state/t", 25165, "kWh", "Some odd title", {"divisor": 1})
        self.assertEqual(config["device_class"], "power")


class TestBuildSensorConfigTitleKeywordMatch(unittest.TestCase):
    """When the unit alone can't resolve a device_class, map_device_class
    falls back to a title keyword scan — a mutation dropping/replacing the
    title argument would only be observable via this fallback path, since
    a unit-resolvable device_class always wins first (Pass 1 beats Pass 2)."""

    def test_unresolvable_unit_falls_back_to_title_keyword(self):
        from nibe_discovery_config import build_sensor_config

        config = {}
        # 'BT1' is a real Nibe sensor-name keyword mapping to 'temperature'
        # (see _SENSOR_KEYWORD_RULES); the unit itself ('zzq') resolves to
        # nothing, so title must be what drives the classification.
        build_sensor_config(config, "state/t", 999901, "zzq", "BT1 sensor", {"divisor": 1})
        self.assertEqual(config["device_class"], "temperature")


class TestBuildSensorConfigSuggestedPrecision(unittest.TestCase):
    def test_precision_set_for_numeric_sensor(self):
        from nibe_discovery_config import build_sensor_config

        config = {}
        metadata = {"divisor": 10, "decimal": 1}
        build_sensor_config(config, "state/t", 999004, "°C", "Temp", metadata)
        self.assertEqual(config["suggested_display_precision"], 1)

    def test_precision_not_set_for_non_numeric_sensor(self):
        """A sensor with no unit (has_numeric_value=False, e.g. an enum
        status sensor) must never get suggested_display_precision — HA
        rejects every state update with a ValueError if it does."""
        from nibe_discovery_config import build_sensor_config

        config = {}
        metadata = {"decimal": 1}
        build_sensor_config(config, "state/t", 999005, "", "Status", metadata)
        self.assertNotIn("suggested_display_precision", config)
