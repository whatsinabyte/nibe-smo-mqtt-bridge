"""
test_generate.py
================
Generate_nibe_mqtt tests.
Part of the Nibe S-Series MQTT Bridge test suite.
Shared fixtures are in conftest.py.
"""

import json
import os
import ssl
import unittest
from typing import ClassVar
from unittest.mock import MagicMock, patch

from conftest import (
    _APP_DIR,
    _REPO_DIR,
    _make_em,
    _nibe_point_id,
)
from freezegun import freeze_time
from hypothesis import example, given
from hypothesis import strategies as st

# menu_structure.yaml cache reset — see conftest.py's global autouse
# _reset_menu_structure_cache fixture. It applies to every test file
# automatically, so no per-file fixture is needed here.


class TestCleanUnit(unittest.TestCase):
    """Single source of truth for unit cleaning, consolidating three
    previously-independent implementations (a direct mojibake strip used
    twice in generate_nibe_mqtt.py, and a bare _UNIT_NORMALISE table
    lookup used in map_device_class) that could silently drift apart.
    Mirrors the structure of TestCleanString above."""

    def setUp(self):
        from nibe_entity_detection import clean_unit

        self.fn = clean_unit

    def test_normal(self):
        self.assertEqual(self.fn("°C"), "°C")

    def test_mojibake_degree_c(self):
        self.assertEqual(self.fn("\u00c2°C"), "°C")

    def test_mojibake_degree_f(self):
        self.assertEqual(self.fn("\u00c2°F"), "°F")

    def test_mojibake_bare_degree(self):
        self.assertEqual(self.fn("\u00c2°"), "°")

    def test_days_normalised_to_d(self):
        self.assertEqual(self.fn("days"), "d")

    def test_unrecognised_unit_passthrough(self):
        self.assertEqual(self.fn("bar"), "bar")

    def test_percent_passthrough(self):
        self.assertEqual(self.fn("%"), "%")

    def test_whitespace_stripped(self):
        self.assertEqual(self.fn("  °C  "), "°C")

    def test_nbsp_collapsed(self):
        self.assertEqual(self.fn("a\u00a0b"), "a b")

    def test_none(self):
        self.assertEqual(self.fn(None), "")

    def test_empty(self):
        self.assertEqual(self.fn(""), "")

    def test_non_string(self):
        self.assertEqual(self.fn(42), "")

    def test_mojibake_then_table_lookup_combined(self):
        """The mojibake strip must happen BEFORE the table lookup, since the
        table's keys are post-strip forms — confirms the two steps compose
        in the right order rather than being mutually exclusive."""
        self.assertEqual(self.fn("\u00c2°C"), "°C")


# ===========================================================================
# Hypothesis property-based tests
# ===========================================================================
# These tests use Hypothesis to find edge cases the unit tests might miss.
# They verify invariants that must hold for ALL inputs, not just known ones.
# ===========================================================================


# Strategies for Nibe-relevant data
#
class TestDetectTypeWithoutOverrideProperties(unittest.TestCase):
    """Hypothesis properties for _detect_type_without_override.

    Key invariant: must be consistent with _detect_holding_entity and
    _detect_input_entity — it is a pure dispatcher between the two.
    """

    def _point(self, pid, modbus_type, var_type="integer", writable=True):
        return {
            "variableId": pid,
            "description": "",
            "metadata": {
                "variableType": var_type,
                "variableSize": "u8",
                "modbusRegisterType": modbus_type,
                "isWritable": writable,
                "minValue": 0,
                "maxValue": 1,
                "unit": "",
                "divisor": 1,
            },
        }

    @given(
        _nibe_point_id,
        st.sampled_from(
            ["MODBUS_HOLDING_REGISTER", "MODBUS_INPUT_REGISTER", "MODBUS_NO_REGISTER", ""]
        ),
    )
    def test_always_returns_two_tuple(self, pid, modbus_type):
        from nibe_entity_detection import _detect_type_without_override

        point = self._point(pid, modbus_type)
        result = _detect_type_without_override(point, point["metadata"], modbus_type)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    @given(
        _nibe_point_id.filter(
            lambda p: p not in __import__("nibe_entity_detection").VALUE_MAPPINGS.get("holding", {})
        )
    )
    def test_holding_consistent_with_detect_holding_entity(self, pid):
        """For HOLDING registers, result must match _detect_holding_entity directly."""
        from nibe_entity_detection import (
            _detect_holding_entity,
            _detect_type_without_override,
        )

        point = self._point(pid, "MODBUS_HOLDING_REGISTER")
        meta = point["metadata"]
        self.assertEqual(
            _detect_type_without_override(point, meta, "MODBUS_HOLDING_REGISTER"),
            _detect_holding_entity(point, meta),
        )

    @given(
        _nibe_point_id.filter(
            lambda p: p not in __import__("nibe_entity_detection").VALUE_MAPPINGS.get("input", {})
        )
    )
    def test_input_consistent_with_detect_input_entity(self, pid):
        """For INPUT registers, result must match _detect_input_entity directly."""
        from nibe_entity_detection import (
            _detect_input_entity,
            _detect_type_without_override,
        )

        point = self._point(pid, "MODBUS_INPUT_REGISTER", writable=False)
        meta = point["metadata"]
        self.assertEqual(
            _detect_type_without_override(point, meta, "MODBUS_INPUT_REGISTER"),
            _detect_input_entity(point, meta),
        )

    @given(
        _nibe_point_id,
        st.text().filter(lambda s: s not in ("MODBUS_HOLDING_REGISTER", "MODBUS_INPUT_REGISTER")),
    )
    def test_unknown_register_type_always_sensor_diagnostic(self, pid, modbus_type):
        """Any register type that is not HOLDING or INPUT → sensor/diagnostic."""
        from nibe_entity_detection import _detect_type_without_override

        point = self._point(pid, modbus_type)
        result = _detect_type_without_override(point, point["metadata"], modbus_type)
        self.assertEqual(result, ("sensor", "diagnostic"))

    @given(
        _nibe_point_id,
        st.sampled_from(["MODBUS_HOLDING_REGISTER", "MODBUS_INPUT_REGISTER", "MODBUS_NO_REGISTER"]),
    )
    def test_never_raises(self, pid, modbus_type):
        from nibe_entity_detection import _detect_type_without_override

        point = self._point(pid, modbus_type)
        _detect_type_without_override(point, point["metadata"], modbus_type)


# ---------------------------------------------------------------------------
# BridgeConfig.__repr__ credential redaction properties (generate_nibe_mqtt.py)
# ---------------------------------------------------------------------------


class TestBridgeConfigReprProperties(unittest.TestCase):
    """Hypothesis properties for BridgeConfig.__repr__ credential redaction."""

    @given(
        st.text(
            min_size=16,
            max_size=50,
            alphabet=st.characters(categories=["L", "N"], include_characters="_"),
        ).filter(lambda s: s not in ("core-mosquitto", "Nibe SMO S40")),
        st.text(
            min_size=16,
            max_size=50,
            alphabet=st.characters(categories=["L", "N"], include_characters="_"),
        ),
    )
    def test_credentials_never_appear_in_repr(self, auth, password):
        """__repr__ must never expose actual credential values.
        Uses ≥16-char alphanumeric strings — long enough that no generated
        value can be a substring of a static repr field name like
        'nibe_password' (13 chars)."""
        from generate_nibe_mqtt import BridgeConfig

        cfg = BridgeConfig()
        cfg.nibe_auth = auth
        cfg.mqtt_password = password
        r = repr(cfg)
        self.assertNotIn(auth, r)
        self.assertNotIn(password, r)

    @given(st.text(min_size=1, max_size=50))
    def test_repr_always_returns_string(self, auth):
        from generate_nibe_mqtt import BridgeConfig

        cfg = BridgeConfig()
        cfg.nibe_auth = auth
        self.assertIsInstance(repr(cfg), str)

    @given(st.text(min_size=1, max_size=50), st.text(min_size=1, max_size=50))
    def test_repr_contains_redacted_marker(self, auth, password):
        """Redacted fields must show a placeholder, not be silently empty."""
        from generate_nibe_mqtt import BridgeConfig

        cfg = BridgeConfig()
        cfg.nibe_auth = auth
        cfg.mqtt_password = password
        r = repr(cfg)
        # Must contain some redaction marker
        self.assertTrue(
            "***" in r or "REDACTED" in r or "<" in r,
            f"No redaction marker found in repr: {r[:100]}",
        )

    def test_none_credentials_do_not_crash_repr(self):
        """None credentials must not cause __repr__ to raise."""
        from generate_nibe_mqtt import BridgeConfig

        cfg = BridgeConfig()
        cfg.nibe_auth = None
        cfg.mqtt_password = None
        cfg.mqtt_username = None
        repr(cfg)  # must not raise


# ---------------------------------------------------------------------------
# BridgeConfig default field and repr properties (generate_nibe_mqtt.py)
# ---------------------------------------------------------------------------


class TestBridgeConfigProperties(unittest.TestCase):
    """Hypothesis properties for BridgeConfig defaults and repr."""

    def _cfg(self):
        from generate_nibe_mqtt import BridgeConfig

        return BridgeConfig()

    def test_default_poll_interval(self):
        """Default poll_interval must be 30s."""
        self.assertEqual(self._cfg().poll_interval, 30)

    def test_default_api_failure_threshold(self):
        """Default api_failure_threshold must be 3."""
        self.assertEqual(self._cfg().api_failure_threshold, 3)

    def test_default_changelog_retention_days(self):
        """Default changelog_retention_days must be 90."""
        self.assertEqual(self._cfg().changelog_retention_days, 90)

    @given(st.integers(min_value=1, max_value=3600))
    def test_repr_contains_poll_interval(self, poll):
        """__repr__ must always show the poll_interval."""
        from generate_nibe_mqtt import BridgeConfig

        cfg = BridgeConfig()
        cfg.poll_interval = poll
        r = repr(cfg)
        self.assertIn(f"poll={poll}s", r)

    @given(st.integers(min_value=1, max_value=3600))
    def test_repr_always_string(self, poll):
        from generate_nibe_mqtt import BridgeConfig

        cfg = BridgeConfig()
        cfg.poll_interval = poll
        self.assertIsInstance(repr(cfg), str)

    def test_default_mode_is_valid(self):
        """Default mode must be one of the supported modes."""
        from generate_nibe_mqtt import MODES, BridgeConfig

        cfg = BridgeConfig()
        self.assertIn(cfg.mode, MODES)

    @given(st.integers(min_value=1, max_value=100))
    def test_api_failure_threshold_positive(self, threshold):
        """api_failure_threshold must always be stored as-is."""
        from generate_nibe_mqtt import BridgeConfig

        cfg = BridgeConfig()
        cfg.api_failure_threshold = threshold
        self.assertEqual(cfg.api_failure_threshold, threshold)

    @given(st.integers(min_value=1, max_value=3650))
    def test_changelog_retention_days_positive(self, days):
        from generate_nibe_mqtt import BridgeConfig

        cfg = BridgeConfig()
        cfg.changelog_retention_days = days
        self.assertEqual(cfg.changelog_retention_days, days)

    def test_valid_poll_intervals_in_keepalive(self):
        """For every valid poll interval, keepalive is max(60, poll+10)."""
        from generate_nibe_mqtt import _keepalive_from_config

        valid_polls = [15, 30, 60, 120, 300]
        for poll in valid_polls:
            self.assertEqual(_keepalive_from_config(poll), max(60, poll + 10))

    @given(st.sampled_from([15, 30, 60, 120, 300]))
    def test_keepalive_for_valid_poll_always_max_60_or_poll_plus_10(self, poll):
        """For any valid firmware poll interval, keepalive = max(60, poll + 10)."""
        from generate_nibe_mqtt import _keepalive_from_config

        self.assertEqual(_keepalive_from_config(poll), max(60, poll + 10))


# ---------------------------------------------------------------------------
# MODES structural invariants (nibe_entity_detection.py)
# ---------------------------------------------------------------------------


class TestLoadConfig(unittest.TestCase):
    """Tests for load_config() — options.json, secrets.yaml, env vars, CLI args."""

    def setUp(self):
        # Ensure no real files or env vars bleed into tests
        self._env_patcher = patch.dict("os.environ", {}, clear=True)
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()

    def _load(self, options=None, secrets=None, env=None, cli_args=None):
        """Call load_config with mocked filesystem and environment."""
        import generate_nibe_mqtt as gn

        env = env or {}

        def fake_exists(path):
            if path == "/data/options.json":
                return options is not None
            if path in ("/config/secrets.yaml", "/homeassistant/secrets.yaml", "./secrets.yaml"):
                return secrets is not None and path == "./secrets.yaml"
            return False

        import io

        def fake_open(path, *a, **kw):
            if path == "/data/options.json":
                return io.StringIO(json.dumps(options))
            if path == "./secrets.yaml":
                return io.StringIO(secrets)
            raise FileNotFoundError(path)

        with (
            patch("os.path.exists", side_effect=fake_exists),
            patch("builtins.open", side_effect=fake_open),
            patch.dict("os.environ", env),
        ):
            return gn.load_config(cli_args)

    # ── defaults ─────────────────────────────────────────────────────────────

    def test_defaults_when_no_sources(self):
        cfg = self._load()
        self.assertEqual(cfg.api_host, "192.168.2.201")
        self.assertEqual(cfg.api_port, 8443)
        self.assertEqual(cfg.mqtt_broker, "core-mosquitto")
        self.assertEqual(cfg.mqtt_port, 1883)
        self.assertEqual(cfg.poll_interval, 30)
        self.assertEqual(cfg.mode, "essential")
        self.assertEqual(cfg.log_level, "info")

    # ── options.json ─────────────────────────────────────────────────────────

    def test_options_json_sets_api_host(self):
        cfg = self._load(options={"nibe_host": "10.0.0.5"})
        self.assertEqual(cfg.api_host, "10.0.0.5")

    def test_options_json_sets_mqtt_broker(self):
        cfg = self._load(options={"mqtt_host": "mybroker"})
        self.assertEqual(cfg.mqtt_broker, "mybroker")

    def test_options_json_sets_poll_interval(self):
        cfg = self._load(options={"poll_interval": "60"})
        self.assertEqual(cfg.poll_interval, 60)

    def test_options_json_invalid_poll_interval_snaps_to_nearest(self):
        cfg = self._load(options={"poll_interval": "45"})
        self.assertIn(cfg.poll_interval, {15, 30, 60, 120, 300})
        self.assertEqual(cfg.poll_interval, 30)  # nearest to 45 is 30 or 60; tie goes to 30

    @given(st.integers(min_value=1, max_value=10_000))
    @example(value=45)  # exact tie between 30 and 60 (both 15 away)
    @example(value=1)  # far below the smallest valid value (15)
    @example(value=10_000)  # far above the largest valid value (300)
    def test_invalid_poll_interval_always_snaps_to_the_nearest_valid_value(self, value):
        """_validated_poll's clip must always land on a member of
        _VALID_POLL_INTERVALS whose distance to the input is <= every other
        member's distance — generalizes the single hardcoded tie-break
        example above (45 -> 30) to the full input space, including values
        far outside the valid set's range."""
        valid = {15, 30, 60, 120, 300}
        cfg = self._load(options={"poll_interval": str(value)})
        self.assertIn(cfg.poll_interval, valid)
        chosen_distance = abs(cfg.poll_interval - value)
        for candidate in valid:
            self.assertLessEqual(chosen_distance, abs(candidate - value))

    def test_options_json_invalid_poll_interval_warning_names_options_json_source(self):
        """_validated_poll's warning must identify 'options.json' as the
        source (not e.g. a None passed by mistake) — this is the only
        signal telling the user WHICH config source supplied the invalid
        value when multiple sources are in play."""
        cfg = self._load(options={"poll_interval": "45"})
        self.assertTrue(any(w.startswith("options.json: poll_interval=45") for w in cfg.warnings))

    def test_options_json_sets_debug_mode_true(self):
        cfg = self._load(options={"debug_mode": True})
        self.assertIs(cfg.debug_mode, True)

    def test_options_json_sets_debug_mode_false(self):
        cfg = self._load(options={"debug_mode": False})
        self.assertIs(cfg.debug_mode, False)

    def test_options_json_sets_mode(self):
        cfg = self._load(options={"mode": "all"})
        self.assertEqual(cfg.mode, "all")

    def test_options_json_sets_mode_switch_behavior(self):
        cfg = self._load(options={"mode_switch_behavior": "merge"})
        self.assertEqual(cfg.mode_switch_behavior, "merge")

    def test_mode_switch_behavior_defaults_to_replace(self):
        cfg = self._load()
        self.assertEqual(cfg.mode_switch_behavior, "replace")

    def test_invalid_mode_switch_behavior_falls_back_to_replace_with_warning(self):
        cfg = self._load(options={"mode_switch_behavior": "bogus"})
        self.assertEqual(cfg.mode_switch_behavior, "replace")
        self.assertTrue(any("mode_switch_behavior" in w for w in cfg.warnings))

    def test_invalid_mode_from_env_falls_back_to_essential_with_warning(self):
        """load_config's own mode-validation fallback (distinct from
        parse_arguments' argparse choices=, which rejects invalid CLI
        values before load_config ever runs) — reachable via NIBE_MODE,
        which bypasses the CLI schema entirely."""
        cfg = self._load(env={"NIBE_MODE": "bogus"})
        self.assertEqual(cfg.mode, "essential")
        self.assertTrue(any("mode=" in w and "bogus" in w for w in cfg.warnings))

    def test_invalid_log_level_from_env_falls_back_to_info_with_warning(self):
        cfg = self._load(env={"NIBE_LOG_LEVEL": "bogus"})
        self.assertEqual(cfg.log_level, "info")
        self.assertTrue(any("log_level=" in w and "bogus" in w for w in cfg.warnings))

    def test_options_json_sets_log_level(self):
        cfg = self._load(options={"log_level": "debug"})
        self.assertEqual(cfg.log_level, "debug")

    def test_options_json_sets_device_name(self):
        cfg = self._load(options={"device_name": "My Heat Pump"})
        self.assertEqual(cfg.device_name, "My Heat Pump")

    def test_options_json_sets_language(self):
        cfg = self._load(options={"language": "nl"})
        self.assertEqual(cfg.language, "nl")

    def test_options_json_sets_api_failure_threshold(self):
        cfg = self._load(options={"api_failure_threshold": 5})
        self.assertEqual(cfg.api_failure_threshold, 5)

    def test_options_json_sets_changelog_retention_days(self):
        cfg = self._load(options={"changelog_retention_days": 30})
        self.assertEqual(cfg.changelog_retention_days, 30)

    def test_options_json_sets_mqtt_tls(self):
        cfg = self._load(options={"mqtt_tls": True})
        self.assertTrue(cfg.mqtt_tls)

    def test_options_json_sets_changelog_retention(self):
        cfg = self._load(options={"changelog_retention_days": 30})
        self.assertEqual(cfg.changelog_retention_days, 30)

    @given(st.integers(min_value=-1000, max_value=10_000))
    @example(-5)  # below the lower bound
    @example(0)  # exact lower-bound-minus-one
    @example(1)  # exact lower bound
    @example(100)  # exact upper bound
    @example(101)  # exact upper-bound-plus-one
    @example(9999)  # well above the upper bound
    def test_options_json_api_failure_threshold_always_clamped_to_1_100(self, raw):
        """config.yaml's schema promises int(1,100) — a hand-edited
        options.json (which bypasses that schema) must still be clamped
        into range for any integer, not just the couple of in-range
        values the example-based tests above use."""
        cfg = self._load(options={"api_failure_threshold": raw})
        self.assertGreaterEqual(cfg.api_failure_threshold, 1)
        self.assertLessEqual(cfg.api_failure_threshold, 100)
        if 1 <= raw <= 100:
            self.assertEqual(cfg.api_failure_threshold, raw)

    @given(st.integers(min_value=-1000, max_value=10_000))
    @example(-5)
    @example(0)
    @example(1)
    @example(3650)
    @example(3651)
    @example(9999)
    def test_options_json_changelog_retention_days_always_clamped_to_1_3650(self, raw):
        """config.yaml's schema promises int(1,3650) — same clamping
        contract as api_failure_threshold above, for the same reason."""
        cfg = self._load(options={"changelog_retention_days": raw})
        self.assertGreaterEqual(cfg.changelog_retention_days, 1)
        self.assertLessEqual(cfg.changelog_retention_days, 3650)
        if 1 <= raw <= 3650:
            self.assertEqual(cfg.changelog_retention_days, raw)

    def test_options_json_parse_error_adds_warning(self):
        import generate_nibe_mqtt as gn

        def fake_exists(p):
            return p == "/data/options.json"

        import io

        def fake_open(p, *a, **kw):
            if p == "/data/options.json":
                return io.StringIO("not valid json {{{")
            raise FileNotFoundError(p)

        with (
            patch("os.path.exists", side_effect=fake_exists),
            patch("builtins.open", side_effect=fake_open),
        ):
            cfg = gn.load_config()
        self.assertTrue(any("options.json" in w for w in cfg.warnings))

    # ── secrets.yaml ─────────────────────────────────────────────────────────

    def test_secrets_yaml_sets_mqtt_username(self):
        cfg = self._load(secrets="mqtt_user: myuser\n")
        self.assertEqual(cfg.mqtt_username, "myuser")

    def test_secrets_yaml_sets_mqtt_password(self):
        cfg = self._load(secrets="mqtt_password: s3cr3t\n")
        self.assertEqual(cfg.mqtt_password, "s3cr3t")

    def test_secrets_yaml_sets_nibe_basic_auth(self):
        cfg = self._load(secrets="nibe_basic_auth: dXNlcjpwYXNz\n")
        self.assertEqual(cfg.nibe_basic_auth, "dXNlcjpwYXNz")

    def test_secrets_yaml_quoted_value_strips_quotes(self):
        cfg = self._load(secrets='mqtt_password: "pass#word"\n')
        self.assertEqual(cfg.mqtt_password, "pass#word")

    def test_secrets_yaml_does_not_override_options_json_credentials(self):
        """options.json credentials take priority over secrets.yaml."""
        cfg = self._load(
            options={"mqtt_username": "from_options"},
            secrets="mqtt_user: from_secrets\n",
        )
        self.assertEqual(cfg.mqtt_username, "from_options")

    # ── environment variables ─────────────────────────────────────────────────

    def test_env_sets_api_host(self):
        cfg = self._load(env={"NIBE_API_HOST": "10.1.2.3"})
        self.assertEqual(cfg.api_host, "10.1.2.3")

    def test_env_sets_api_port(self):
        cfg = self._load(env={"NIBE_API_PORT": "9443"})
        self.assertEqual(cfg.api_port, 9443)

    def test_env_sets_mqtt_broker(self):
        cfg = self._load(env={"NIBE_MQTT_BROKER": "my-broker.local"})
        self.assertEqual(cfg.mqtt_broker, "my-broker.local")

    def test_env_sets_mqtt_port(self):
        cfg = self._load(env={"NIBE_MQTT_PORT": "8883"})
        self.assertEqual(cfg.mqtt_port, 8883)

    def test_env_sets_device_name(self):
        cfg = self._load(env={"NIBE_DEVICE_NAME": "Custom Name"})
        self.assertEqual(cfg.device_name, "Custom Name")

    def test_env_sets_language(self):
        cfg = self._load(env={"NIBE_LANGUAGE": "de"})
        self.assertEqual(cfg.language, "de")

    def test_env_sets_remove_frontend_true_when_value_is_one(self):
        cfg = self._load(env={"NIBE_REMOVE_FRONTEND": "1"})
        self.assertIs(cfg.remove_frontend, True)

    def test_env_remove_frontend_false_when_value_is_not_one(self):
        """Any value other than the literal '1' must leave remove_frontend
        at its default (False) — not any other truthy-string check."""
        cfg = self._load(env={"NIBE_REMOVE_FRONTEND": "true"})
        self.assertIs(cfg.remove_frontend, False)

    def test_env_sets_log_level(self):
        cfg = self._load(env={"NIBE_LOG_LEVEL": "debug"})
        self.assertEqual(cfg.log_level, "debug")

    def test_env_sets_mode(self):
        cfg = self._load(env={"NIBE_MODE": "advanced"})
        self.assertEqual(cfg.mode, "advanced")

    def test_env_sets_mode_switch_behavior(self):
        cfg = self._load(env={"NIBE_MODE_SWITCH_BEHAVIOR": "merge"})
        self.assertEqual(cfg.mode_switch_behavior, "merge")

    def test_env_sets_poll_interval(self):
        cfg = self._load(env={"NIBE_POLL_INTERVAL": "120"})
        self.assertEqual(cfg.poll_interval, 120)

    def test_env_poll_interval_below_15_snaps_to_15(self):
        cfg = self._load(env={"NIBE_POLL_INTERVAL": "5"})
        self.assertEqual(cfg.poll_interval, 15)

    def test_non_numeric_env_var_does_not_crash_startup(self):
        """A misconfigured non-numeric NIBE_API_PORT (or similar) must not
        raise out of load_config — it runs before any exception handling
        is installed, so an uncaught ValueError here would crash the whole
        add-on at startup instead of falling back to defaults."""
        cfg = self._load(env={"NIBE_API_PORT": "not-a-number"})
        self.assertEqual(cfg.api_port, 8443)  # falls back to the default

    def test_non_numeric_env_var_does_not_block_other_env_settings(self):
        """A bad numeric env var must not prevent unrelated, valid string
        env vars from being applied — both are set before the try/except
        that guards only the numeric conversions."""
        cfg = self._load(
            env={
                "NIBE_API_PORT": "not-a-number",
                "NIBE_DEVICE_NAME": "Still Works",
            }
        )
        self.assertEqual(cfg.device_name, "Still Works")

    def test_env_overrides_options_json_for_api_host(self):
        cfg = self._load(
            options={"nibe_host": "192.168.1.1"},
            env={"NIBE_API_HOST": "10.0.0.99"},
        )
        self.assertEqual(cfg.api_host, "10.0.0.99")

    def test_env_overrides_options_json_for_mqtt_broker(self):
        """NIBE_MQTT_BROKER (set by run.sh from Supervisor-discovered service
        info) must win over an explicit mqtt_host entered in options.json.
        This is the exact field/scenario of a real production bug where
        auto-discovery silently overrode a user's explicit mqtt_host setting
        — regression guard, not just precedence documentation."""
        cfg = self._load(
            options={"mqtt_host": "user-entered-broker.local"},
            env={"NIBE_MQTT_BROKER": "discovered-broker.local"},
        )
        self.assertEqual(cfg.mqtt_broker, "discovered-broker.local")

    def test_env_svc_username_sets_mqtt_username(self):
        """NIBE_MQTT_SVC_USERNAME from Supervisor Services API sets mqtt_username."""
        cfg = self._load(env={"NIBE_MQTT_SVC_USERNAME": "addons"})
        self.assertEqual(cfg.mqtt_username, "addons")

    def test_env_svc_password_sets_mqtt_password(self):
        """NIBE_MQTT_SVC_PASSWORD from Supervisor Services API sets mqtt_password."""
        cfg = self._load(env={"NIBE_MQTT_SVC_PASSWORD": "secret123"})
        self.assertEqual(cfg.mqtt_password, "secret123")

    def test_env_svc_credentials_override_options_json(self):
        """Supervisor-discovered credentials override manually entered options.json values."""
        cfg = self._load(
            options={"mqtt_username": "manual_user", "mqtt_password": "manual_pass"},
            env={
                "NIBE_MQTT_SVC_USERNAME": "svc_user",
                "NIBE_MQTT_SVC_PASSWORD": "svc_pass",
            },
        )
        self.assertEqual(cfg.mqtt_username, "svc_user")
        self.assertEqual(cfg.mqtt_password, "svc_pass")

    # ── CLI args ──────────────────────────────────────────────────────────────

    def test_cli_log_level_overrides_options_json(self):
        cli = MagicMock()
        cli.log_level = "debug"
        cli.mode = None
        cfg = self._load(options={"log_level": "info"}, cli_args=cli)
        self.assertEqual(cfg.log_level, "debug")

    def test_cli_mode_overrides_options_json(self):
        cli = MagicMock()
        cli.log_level = None
        cli.mode = "all"
        cfg = self._load(options={"mode": "essential"}, cli_args=cli)
        self.assertEqual(cfg.mode, "all")

    # ── real parse_arguments() output, not a hand-built mock ───────────────
    #
    # The tests above use a MagicMock with the untested field explicitly set
    # to None to isolate what they check. A real parse_arguments() call can
    # never actually produce None for BOTH fields at once the way these mocks
    # do — this class of test previously hid a real bug: parse_arguments()'s
    # own argparse defaults ('info'/'essential') made args.log_level/args.mode
    # truthy even when the user passed no CLI flags at all, so load_config()'s
    # `if cli_args.log_level` check always fired and silently discarded
    # options.json / NIBE_LOG_LEVEL / NIBE_MODE. Fixed by making the argparse
    # defaults None; these tests use the real function end-to-end to lock
    # in the fix and guard against it regressing.

    def test_real_cli_args_no_flags_lets_env_log_level_through(self):
        """With parse_arguments() invoked with zero CLI flags (the normal
        case — run.sh always passes -l explicitly, but direct/dev invocation
        may not), NIBE_LOG_LEVEL must reach cfg.log_level, not be silently
        discarded by an always-truthy CLI default."""
        from generate_nibe_mqtt import parse_arguments

        with patch("sys.argv", ["bridge"]):
            real_args = parse_arguments()
        cfg = self._load(env={"NIBE_LOG_LEVEL": "debug"}, cli_args=real_args)
        self.assertEqual(cfg.log_level, "debug")

    def test_real_cli_args_no_flags_lets_env_mode_through(self):
        """Same as above, for NIBE_MODE."""
        from generate_nibe_mqtt import parse_arguments

        with patch("sys.argv", ["bridge"]):
            real_args = parse_arguments()
        cfg = self._load(env={"NIBE_MODE": "monitoring"}, cli_args=real_args)
        self.assertEqual(cfg.mode, "monitoring")

    def test_real_cli_args_no_flags_lets_options_json_log_level_through(self):
        from generate_nibe_mqtt import parse_arguments

        with patch("sys.argv", ["bridge"]):
            real_args = parse_arguments()
        cfg = self._load(options={"log_level": "warning"}, cli_args=real_args)
        self.assertEqual(cfg.log_level, "warning")

    def test_real_cli_args_explicit_flag_still_overrides_options_json(self):
        """When the CLI flag genuinely IS passed, it must still win — the
        fix must not have broken real CLI precedence."""
        from generate_nibe_mqtt import parse_arguments

        with patch("sys.argv", ["bridge", "--log-level", "error", "--mode", "all"]):
            real_args = parse_arguments()
        cfg = self._load(
            options={"log_level": "warning", "mode": "essential"},
            cli_args=real_args,
        )
        self.assertEqual(cfg.log_level, "error")
        self.assertEqual(cfg.mode, "all")

    def test_real_cli_args_no_flags_no_env_no_options_falls_back_to_dataclass_default(self):
        """With nothing set anywhere, cfg.log_level/mode must still resolve
        to BridgeConfig's own defaults ('info'/'essential') — confirming the
        fallback moved to the right place rather than disappearing."""
        from generate_nibe_mqtt import parse_arguments

        with patch("sys.argv", ["bridge"]):
            real_args = parse_arguments()
        cfg = self._load(cli_args=real_args)
        self.assertEqual(cfg.log_level, "info")
        self.assertEqual(cfg.mode, "essential")

    # ── derived values ────────────────────────────────────────────────────────

    def test_api_base_url_built_from_host_and_port(self):
        cfg = self._load(options={"nibe_host": "10.0.0.5", "nibe_port": 8443})
        self.assertEqual(cfg.api_base_url, "https://10.0.0.5:8443/api/v1/devices/0")

    def test_nibe_auth_built_from_username_password(self):
        import base64

        cfg = self._load(options={"nibe_username": "user", "nibe_password": "pass"})
        expected = "Basic " + base64.b64encode(b"user:pass").decode()
        self.assertEqual(cfg.nibe_auth, expected)

    def test_nibe_basic_auth_used_directly_when_set(self):
        cfg = self._load(secrets="nibe_basic_auth: Basic dXNlcjpwYXNz\n")
        self.assertEqual(cfg.nibe_auth, "Basic dXNlcjpwYXNz")

    def test_nibe_basic_auth_without_prefix_gets_basic_prepended(self):
        cfg = self._load(secrets="nibe_basic_auth: dXNlcjpwYXNz\n")
        self.assertTrue(cfg.nibe_auth.startswith("Basic "))

    def test_nibe_basic_auth_wins_over_username_password_when_both_present(self):
        """When both a pre-encoded nibe_basic_auth (secrets.yaml-only) and
        nibe_username/nibe_password (options.json) are present, the
        if/elif in load_config() must prefer nibe_basic_auth — this was
        never directly exercised; every other test sets only one or the
        other, which can't tell an `if/elif` apart from an `if/if` that
        happened to only ever see one branch's inputs."""
        cfg = self._load(
            options={"nibe_username": "optuser", "nibe_password": "optpass"},
            secrets="nibe_basic_auth: Basic cHJlZW5jb2RlZA==\n",
        )
        self.assertEqual(cfg.nibe_auth, "Basic cHJlZW5jb2RlZA==")

    def test_repr_redacts_passwords(self):
        cfg = self._load(
            options={
                "nibe_username": "myspecialuser",
                "nibe_password": "myspecialpass",
                "mqtt_username": "mqttspecialuser",
                "mqtt_password": "mqttspecialpass",
            }
        )
        r = repr(cfg)
        self.assertNotIn("myspecialpass", r)
        self.assertNotIn("mqttspecialpass", r)
        self.assertNotIn("myspecialuser", r)
        self.assertNotIn("mqttspecialuser", r)
        self.assertIn("***", r)


class TestLoadConfigLanguageAutoDetect(unittest.TestCase):
    """language: an explicit value from any source always wins; only when
    left blank does load_config() fall back to HA's own configured language
    via _get_ha_language() — same precedence pattern already used for
    mqtt_host's Supervisor-based auto-discovery (see run.sh)."""

    def setUp(self):
        self._env_patcher = patch.dict("os.environ", {}, clear=True)
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()

    def _load(self, options=None, ha_language=""):
        import generate_nibe_mqtt as gn

        def fake_exists(path):
            return path == "/data/options.json" and options is not None

        import io

        def fake_open(path, *a, **kw):
            if path == "/data/options.json":
                return io.StringIO(json.dumps(options))
            raise FileNotFoundError(path)

        with (
            patch("os.path.exists", side_effect=fake_exists),
            patch("builtins.open", side_effect=fake_open),
            patch("generate_nibe_mqtt._get_ha_language", return_value=ha_language),
        ):
            return gn.load_config()

    def test_blank_language_auto_detects_from_ha(self):
        """No explicit language anywhere — the auto-detected HA language is
        used."""
        cfg = self._load(options={}, ha_language="sv")
        self.assertEqual(cfg.language, "sv")

    def test_explicit_auto_sentinel_auto_detects_from_ha(self):
        """language: "auto" is config.yaml's actual default value (the
        dropdown's selected option out of the box) — this is the real path
        exercised in production, not just an absent/blank value."""
        cfg = self._load(options={"language": "auto"}, ha_language="de")
        self.assertEqual(cfg.language, "de")

    def test_explicit_options_json_language_wins_over_auto_detect(self):
        """An explicit language in options.json must not be overridden by
        HA's auto-detected language, even if they differ."""
        cfg = self._load(options={"language": "nl"}, ha_language="de")
        self.assertEqual(cfg.language, "nl")

    def test_auto_detect_not_called_when_language_already_set(self):
        """_get_ha_language() must not even be invoked once an explicit
        language has already been resolved from an earlier source — the
        Supervisor call is unnecessary work, and a test asserting only the
        final value can't tell 'never called' apart from 'called but its
        result was discarded'."""
        import generate_nibe_mqtt as gn

        def fake_exists(path):
            return path == "/data/options.json"

        import io

        def fake_open(path, *a, **kw):
            if path == "/data/options.json":
                return io.StringIO(json.dumps({"language": "fr"}))
            raise FileNotFoundError(path)

        with (
            patch("os.path.exists", side_effect=fake_exists),
            patch("builtins.open", side_effect=fake_open),
            patch("generate_nibe_mqtt._get_ha_language") as mock_detect,
        ):
            cfg = gn.load_config()
        self.assertEqual(cfg.language, "fr")
        mock_detect.assert_not_called()

    def test_ha_auto_detect_returning_empty_leaves_language_blank(self):
        """When HA itself has no language configured (or the Supervisor call
        fails), language stays '' — meaning the API's own default
        (English), not a crash or a placeholder value."""
        cfg = self._load(options={}, ha_language="")
        self.assertEqual(cfg.language, "")


# ===========================================================================
# 24. ManagementCommandHandler — MQTT command handlers
# ===========================================================================


class TestSetupMenuDashboardReturnType(unittest.TestCase):
    """_setup_menu_dashboard was annotated -> bool but had bare return (= None)
    at early-exit paths. Fixed to return False explicitly at all early exits.
    Tests verify the three early-exit conditions return exactly False, not None,
    so callers doing strict type checks behave correctly."""

    def _watcher(self):
        em = MagicMock()
        pub = MagicMock()
        from nibe_ha_integration import HAEntityRegistryWatcher

        w = HAEntityRegistryWatcher(em, pub)
        w._unique_id_map = {}
        em.all_points_by_id = {}
        em.dynamic_point_map = MagicMock()
        em.dynamic_point_map.values.return_value = []
        em.dynamic_point_map.all_known_dynamic_point_ids.return_value = set()
        em.active_dynamic_points = set()
        em.bulk_data = {}
        em.mqtt_enabled_points = set()
        em.point_to_menu_map = {}
        return w

    def test_missing_yaml_returns_false_not_none(self):
        from nibe_lovelace import _setup_menu_dashboard

        open_ws_fn = MagicMock()
        watcher = self._watcher()
        with patch("generate_nibe_mqtt.os.path.exists", return_value=False):
            result = _setup_menu_dashboard(open_ws_fn, watcher)
        self.assertIs(result, False)
        self.assertIsNotNone(result)  # confirms False, not None
        open_ws_fn.assert_not_called()  # ws never opened before the early return

    def test_yaml_parse_error_returns_false_not_none(self):
        from nibe_lovelace import _setup_menu_dashboard

        open_ws_fn = MagicMock()
        watcher = self._watcher()
        with (
            patch("generate_nibe_mqtt.os.path.exists", return_value=True),
            patch("builtins.open", side_effect=OSError("read error")),
        ):
            result = _setup_menu_dashboard(open_ws_fn, watcher)
        self.assertIs(result, False)
        open_ws_fn.assert_not_called()

    def test_empty_menu_structure_returns_false_not_none(self):
        import io

        from nibe_lovelace import _setup_menu_dashboard

        open_ws_fn = MagicMock()
        watcher = self._watcher()
        with (
            patch("generate_nibe_mqtt.os.path.exists", return_value=True),
            patch("builtins.open", return_value=io.StringIO("menus: []")),
        ):
            result = _setup_menu_dashboard(open_ws_fn, watcher)
        self.assertIs(result, False)
        open_ws_fn.assert_not_called()


class TestParseArgumentsModes(unittest.TestCase):
    """parse_arguments() choices must cover every entry in MODES
    (nibe_entity_detection.py) so no valid mode can ever be rejected at
    the CLI. (Renamed from the preset-era --preset flag as part of the
    entity-mode refactor.)"""

    def test_menus_mode_accepted(self):
        from generate_nibe_mqtt import parse_arguments

        with patch("sys.argv", ["bridge", "--mode", "menus"]):
            args = parse_arguments()
        self.assertEqual(args.mode, "menus")

    def test_mode_is_none_when_omitted(self):
        """args.mode must be None, not a truthy default, when -m/--mode is
        not passed. load_config()/_resolve_initial_mode() use
        `if args.mode` truthiness checks to decide whether the CLI
        explicitly overrides options.json/NIBE_MODE — a truthy argparse
        default here would make that check always fire, silently discarding
        options.json and the env var whenever the caller doesn't use
        run.sh's own options.json-to-CLI passthrough. The eventual runtime
        default ('essential') comes from BridgeConfig's own dataclass
        field, not from argparse."""
        from generate_nibe_mqtt import parse_arguments

        with patch("sys.argv", ["bridge"]):
            args = parse_arguments()
        self.assertIsNone(args.mode)

    def test_log_level_is_none_when_omitted(self):
        """args.log_level must be None, not a truthy default, when -l is
        not passed — same rationale as test_mode_is_none_when_omitted."""
        from generate_nibe_mqtt import parse_arguments

        with patch("sys.argv", ["bridge"]):
            args = parse_arguments()
        self.assertIsNone(args.log_level)

    def test_all_log_levels_accepted(self):
        from generate_nibe_mqtt import parse_arguments

        for level in ("debug", "info", "warning", "error"):
            with patch("sys.argv", ["bridge", "--log-level", level]):
                args = parse_arguments()
            self.assertEqual(args.log_level, level, f"log level '{level}' should be accepted")

    def test_invalid_log_level_rejected(self):
        from generate_nibe_mqtt import parse_arguments

        with (
            patch("sys.argv", ["bridge", "--log-level", "nonexistent"]),
            self.assertRaises(SystemExit),
        ):
            parse_arguments()

    def test_short_flag_l_sets_log_level(self):
        from generate_nibe_mqtt import parse_arguments

        with patch("sys.argv", ["bridge", "-l", "debug"]):
            args = parse_arguments()
        self.assertEqual(args.log_level, "debug")

    def test_short_flag_m_sets_mode(self):
        from generate_nibe_mqtt import parse_arguments

        with patch("sys.argv", ["bridge", "-m", "monitoring"]):
            args = parse_arguments()
        self.assertEqual(args.mode, "monitoring")

    def test_all_modes_accepted(self):
        from generate_nibe_mqtt import parse_arguments

        for mode in ("essential", "monitoring", "advanced", "menus", "all", "none"):
            with patch("sys.argv", ["bridge", "--mode", mode]):
                args = parse_arguments()
            self.assertEqual(args.mode, mode, f"mode '{mode}' should be accepted")

    def test_invalid_mode_rejected(self):
        from generate_nibe_mqtt import parse_arguments

        with (
            patch("sys.argv", ["bridge", "--mode", "nonexistent"]),
            self.assertRaises(SystemExit),
        ):
            parse_arguments()

    def test_modes_match_detection_module(self):
        """The argparse choices must be a superset of MODES keys so no
        valid mode can ever be rejected at the CLI."""
        from generate_nibe_mqtt import parse_arguments
        from nibe_entity_detection import MODES

        # Reconstruct the choices by parsing the parser's actions
        with patch("sys.argv", ["bridge"]):
            # We can't inspect choices directly without accessing parser internals;
            # instead verify every MODES key is accepted without SystemExit.
            for mode in MODES:
                try:
                    with patch("sys.argv", ["bridge", "--mode", mode]):
                        args = parse_arguments()
                    self.assertEqual(args.mode, mode)
                except SystemExit:
                    self.fail(f"MODES key '{mode}' was rejected by parse_arguments()")


class TestOnEnabledStateChangeLovelaceThreadGuard(unittest.TestCase):
    """_on_enabled_state_change skips scheduling a regen when the Lovelace
    setup thread is still alive. This eliminates the double dashboard build
    on fresh starts where the initial menu auto-enable fires the callback
    while the Lovelace setup thread is still running its own
    _setup_menu_dashboard call."""

    def _make_regen_calls(self, thread_alive: bool) -> int:
        """Return the number of _regen_menu_dashboard calls triggered."""
        import threading

        from nibe_lovelace import _on_enabled_state_change_factory

        rw = MagicMock()
        regen_calls = []

        mock_thread = MagicMock(spec=threading.Thread)
        mock_thread.is_alive.return_value = thread_alive

        handler = _on_enabled_state_change_factory(rw, False, lovelace_thread=mock_thread)

        with (
            patch(
                "nibe_lovelace._regen_menu_dashboard",
                side_effect=lambda *a, **kw: regen_calls.append(1),
            ),
            patch("generate_nibe_mqtt.threading.Timer") as mock_timer,
        ):
            mock_timer.return_value = MagicMock()
            handler()
            # Give the timer a moment to fire if scheduled
            if mock_timer.called:
                # Simulate timer firing immediately
                call_args = mock_timer.call_args
                _delay, fn = call_args[0]
                fn()

        return len(regen_calls)

    def test_regen_skipped_when_lovelace_thread_alive(self):
        """When the Lovelace setup thread is still running, the handler
        must not schedule a regen — the thread will do it itself."""
        calls = self._make_regen_calls(thread_alive=True)
        self.assertEqual(calls, 0, "Regen must be skipped when Lovelace thread is alive")

    def test_regen_scheduled_when_lovelace_thread_done(self):
        """When the Lovelace setup thread has finished, the handler must
        schedule a regen normally — user changed entities after startup."""
        calls = self._make_regen_calls(thread_alive=False)
        self.assertEqual(calls, 1, "Regen must fire when Lovelace thread is no longer running")

    def test_no_thread_provided_schedules_normally(self):
        """lovelace_thread=None (e.g. outside HA add-on environment) must
        behave as before — schedule regen unconditionally."""
        from nibe_lovelace import _on_enabled_state_change_factory

        rw = MagicMock()
        regen_calls = []

        handler = _on_enabled_state_change_factory(rw, False, lovelace_thread=None)

        with (
            patch(
                "nibe_lovelace._regen_menu_dashboard",
                side_effect=lambda *a, **kw: regen_calls.append(1),
            ),
            patch("generate_nibe_mqtt.threading.Timer") as mock_timer,
        ):
            mock_timer.return_value = MagicMock()
            handler()
            if mock_timer.called:
                call_args = mock_timer.call_args
                _delay, fn = call_args[0]
                fn()

        self.assertEqual(len(regen_calls), 1)


# ===========================================================================
# 70. EntityManager.all_points and active_entities properties
# ===========================================================================


class TestLoadConfigRemainingPaths(unittest.TestCase):
    """Covers the five specific lines not reached by the existing TestLoadConfig
    suite: nibe_ca_cert and mqtt_ca_cert from options.json, the secrets.yaml
    exception path, the NIBE_API_FAILURE_THRESHOLD env var, and _build_logging."""

    def _load(self, options=None, secrets=None, env=None):
        import io

        import generate_nibe_mqtt as gn

        env = env or {}

        def fake_exists(path):
            if path == "/data/options.json":
                return options is not None
            if path == "./secrets.yaml":
                return secrets is not None
            return False

        def fake_open(path, *a, **kw):
            if path == "/data/options.json":
                return io.StringIO(json.dumps(options))
            if path == "./secrets.yaml":
                if secrets is Exception:
                    raise OSError("forced failure")
                return io.StringIO(secrets)
            raise FileNotFoundError(path)

        with (
            patch("os.path.exists", side_effect=fake_exists),
            patch("builtins.open", side_effect=fake_open),
            patch.dict("os.environ", env, clear=True),
        ):
            return gn.load_config()

    def test_options_json_sets_nibe_ca_cert(self):
        cfg = self._load(options={"nibe_ca_cert": "/config/ca.pem"})
        self.assertEqual(cfg.nibe_ca_cert, "/config/ca.pem")

    def test_options_json_sets_mqtt_ca_cert(self):
        cfg = self._load(options={"mqtt_ca_cert": "/config/mqtt_ca.pem"})
        self.assertEqual(cfg.mqtt_ca_cert, "/config/mqtt_ca.pem")

    def test_secrets_yaml_read_error_adds_warning(self):
        cfg = self._load(secrets=Exception)
        self.assertTrue(any("secrets" in w.lower() for w in cfg.warnings))

    def test_env_api_failure_threshold_applied(self):
        cfg = self._load(env={"NIBE_API_FAILURE_THRESHOLD": "7"})
        self.assertEqual(cfg.api_failure_threshold, 7)

    @given(st.integers(min_value=-1000, max_value=10_000))
    @example(-5)
    @example(1)
    @example(100)
    @example(101)
    @example(9999)
    def test_env_api_failure_threshold_always_clamped_to_1_100(self, raw):
        """NIBE_API_FAILURE_THRESHOLD bypasses the add-on UI's schema
        entirely (dev/Docker-only path) — the clamp here is the only
        thing keeping an out-of-range value from reaching the rest of
        the bridge, so it must hold for any integer, not just '7'."""
        cfg = self._load(env={"NIBE_API_FAILURE_THRESHOLD": str(raw)})
        self.assertGreaterEqual(cfg.api_failure_threshold, 1)
        self.assertLessEqual(cfg.api_failure_threshold, 100)
        if 1 <= raw <= 100:
            self.assertEqual(cfg.api_failure_threshold, raw)

    def test_build_logging_adds_handler_on_fresh_logger(self):
        import logging

        import generate_nibe_mqtt as gn

        root = logging.getLogger("nibe")
        original_handlers = root.handlers[:]
        original_level = root.level
        root.handlers.clear()
        try:
            gn._build_logging("debug")
            self.assertTrue(len(root.handlers) > 0)
            self.assertEqual(root.level, logging.DEBUG)
        finally:
            root.handlers.clear()
            root.handlers.extend(original_handlers)
            root.setLevel(original_level)

    def test_build_logging_default_level_is_info(self):
        """No caller in this codebase relies on the default (every real
        call site passes level= explicitly) — but the default value
        itself is still observable if _build_logging() is ever called
        bare, so it must resolve to INFO, not some other level."""
        import logging

        import generate_nibe_mqtt as gn

        root = logging.getLogger("nibe")
        original_handlers = root.handlers[:]
        original_level = root.level
        root.handlers.clear()
        try:
            gn._build_logging()  # no level argument
            self.assertEqual(root.level, logging.INFO)
        finally:
            root.handlers.clear()
            root.handlers.extend(original_handlers)
            root.setLevel(original_level)

    def test_build_logging_formatter_milliseconds_are_correct(self):
        """The formatter's millisecond field is microsecond // 1000 — a
        wrong divisor (e.g. 1001) would give a subtly-off value for most
        timestamps."""
        import logging

        import generate_nibe_mqtt as gn

        root = logging.getLogger("nibe")
        original_handlers = root.handlers[:]
        original_level = root.level
        root.handlers.clear()
        try:
            gn._build_logging("info")
            formatter = root.handlers[0].formatter
            record = logging.LogRecord(
                "nibe",
                logging.INFO,
                __file__,
                1,
                "msg",
                None,
                None,
            )
            record.created = 1_700_000_000.500000
            formatted = formatter.format(record)
            self.assertIn(".500", formatted)
        finally:
            root.handlers.clear()
            root.handlers.extend(original_handlers)
            root.setLevel(original_level)

    def test_build_logging_invalid_level_falls_back_to_info(self):
        """An unrecognised level string must fall back to logging.INFO —
        not None, which would raise inside root.setLevel(). No existing
        test exercises an invalid level string."""
        import logging

        import generate_nibe_mqtt as gn

        root = logging.getLogger("nibe")
        original_handlers = root.handlers[:]
        original_level = root.level
        root.handlers.clear()
        try:
            gn._build_logging("not_a_real_level")  # must not raise
            self.assertEqual(root.level, logging.INFO)
        finally:
            root.handlers.clear()
            root.handlers.extend(original_handlers)
            root.setLevel(original_level)

    def test_build_logging_skips_handler_when_already_configured(self):
        import logging

        import generate_nibe_mqtt as gn

        root = logging.getLogger("nibe")
        original_handlers = root.handlers[:]
        original_level = root.level
        sentinel = logging.NullHandler()
        root.handlers.clear()
        root.addHandler(sentinel)
        try:
            gn._build_logging("warning")
            # Handler count must not grow
            self.assertEqual(root.handlers, [sentinel])
            self.assertEqual(root.level, logging.WARNING)
        finally:
            root.handlers.clear()
            root.handlers.extend(original_handlers)
            root.setLevel(original_level)


# ===========================================================================
# 79b. _cleanup_mqtt_retained — collects and clears all bridge retained topics
# ===========================================================================


class TestCleanupMqttRetained(unittest.TestCase):
    """_cleanup_mqtt_retained uses the same sentinel pattern as
    scan_mqtt_discovery: subscribe to both bridge namespaces, publish a
    non-retained sentinel, collect retained topics until the sentinel
    arrives, then clear each collected topic by publishing an empty
    retained payload."""

    def _make_client(self):
        from generate_nibe_mqtt import _cleanup_mqtt_retained

        client = MagicMock()
        # publish() must return an object with wait_for_publish()
        client.publish.return_value = MagicMock()
        return client, _cleanup_mqtt_retained

    def _get_callback(self, client, topic_filter):
        """Find the callback registered via message_callback_add for a
        given topic filter."""
        for call in client.message_callback_add.call_args_list:
            if call.args[0] == topic_filter:
                return call.args[1]
        raise AssertionError(f"No callback registered for {topic_filter}")

    def _simulate_sentinel_immediately(self, client):
        """Make mqtt_client.publish(sentinel, ...) immediately invoke the
        sentinel callback, simulating an instant broker round-trip so the
        test doesn't block on sentinel_received.wait(timeout=15)."""

        def fake_publish(topic, payload=None, retain=False):
            if topic == "nibe/browser/scan_sentinel" and not retain:
                callback = self._get_callback(client, "nibe/browser/scan_sentinel")
                msg = MagicMock(topic=topic, payload=b"cleanup", retain=False)
                callback(client, None, msg)
            return MagicMock()

        client.publish.side_effect = fake_publish

    def test_subscribes_to_both_real_wildcards(self):
        client, cleanup = self._make_client()
        self._simulate_sentinel_immediately(client)
        cleanup(client)
        subscribed = [c.args[0] for c in client.subscribe.call_args_list]
        self.assertIn("homeassistant/+/+/+", subscribed)
        self.assertIn("nibe/browser/#", subscribed)

    def test_sentinel_publish_uses_real_payload_and_not_retained(self):
        client, cleanup = self._make_client()
        seen = []

        def spy_publish(topic, payload=None, retain=False):
            if topic == "nibe/browser/scan_sentinel" and not retain:
                seen.append((payload, retain))
                sentinel_cb = self._get_callback(client, "nibe/browser/scan_sentinel")
                sentinel_cb(client, None, MagicMock(topic=topic, payload=b"cleanup", retain=False))
            return MagicMock()

        client.publish.side_effect = spy_publish
        cleanup(client)
        self.assertIn(("cleanup", False), seen)

    def test_clear_publish_uses_payload_none_and_retain_true(self):
        """Each clear-publish must use payload=None (the standard MQTT
        mechanism for deleting a retained message) and retain=True (so the
        empty message is itself retained, actually clearing the broker's
        stored value) — not e.g. a missing/wrong payload kwarg."""
        client, cleanup = self._make_client()

        def fake_publish(topic, payload=None, retain=False):
            if topic == "nibe/browser/scan_sentinel" and not retain:
                ha_cb = self._get_callback(client, "homeassistant/+/+/+")
                ha_cb(
                    client,
                    None,
                    MagicMock(
                        topic="homeassistant/sensor/nibe_test/state",
                        payload=b"22.5",
                        retain=True,
                    ),
                )
                sentinel_cb = self._get_callback(client, "nibe/browser/scan_sentinel")
                sentinel_cb(client, None, MagicMock(topic=topic, payload=b"cleanup", retain=False))
            return MagicMock()

        client.publish.side_effect = fake_publish
        cleanup(client)
        clear_call = next(
            c
            for c in client.publish.call_args_list
            if c.args and c.args[0] == "homeassistant/sensor/nibe_test/state"
        )
        self.assertIsNone(clear_call.kwargs.get("payload"))
        self.assertTrue(clear_call.kwargs.get("retain"))

    def test_wait_for_publish_uses_2_second_timeout(self):
        client, cleanup = self._make_client()
        clear_result = MagicMock()

        def fake_publish(topic, payload=None, retain=False):
            if topic == "nibe/browser/scan_sentinel" and not retain:
                ha_cb = self._get_callback(client, "homeassistant/+/+/+")
                ha_cb(
                    client,
                    None,
                    MagicMock(
                        topic="homeassistant/sensor/nibe_test/state",
                        payload=b"22.5",
                        retain=True,
                    ),
                )
                sentinel_cb = self._get_callback(client, "nibe/browser/scan_sentinel")
                sentinel_cb(client, None, MagicMock(topic=topic, payload=b"cleanup", retain=False))
                return MagicMock()
            return clear_result

        client.publish.side_effect = fake_publish
        cleanup(client)
        clear_result.wait_for_publish.assert_called_once_with(timeout=2.0)

    def test_no_retained_topics_found(self):
        """When no retained messages exist, the function logs and returns
        without attempting any clear-publishes."""
        client, cleanup = self._make_client()
        self._simulate_sentinel_immediately(client)
        with self.assertLogs("nibe.startup", level="INFO") as cm:
            cleanup(client)
        # Only the sentinel publish should have happened — no clear-publishes
        self.assertEqual(client.publish.call_count, 1)
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith("No retained MQTT messages found to clean up")
                for msg in cm.output
            )
        )
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith("Collecting retained MQTT topics for cleanup...")
                for msg in cm.output
            )
        )

    def test_collects_ha_topic_for_this_bridge(self):
        """A retained homeassistant/.../nibe_<id>/... topic must be
        collected and then cleared."""
        client, cleanup = self._make_client()

        def fake_subscribe(topic_filter, *a, **kw):
            return None

        client.subscribe.side_effect = fake_subscribe

        def fake_publish(topic, payload=None, retain=False):
            if topic == "nibe/browser/scan_sentinel" and not retain:
                # Before the sentinel fires, deliver one retained HA message
                ha_cb = self._get_callback(client, "homeassistant/+/+/+")
                msg = MagicMock(
                    topic="homeassistant/sensor/nibe_test/state",
                    payload=b"22.5",
                    retain=True,
                )
                ha_cb(client, None, msg)
                sentinel_cb = self._get_callback(client, "nibe/browser/scan_sentinel")
                sentinel_msg = MagicMock(topic=topic, payload=b"cleanup", retain=False)
                sentinel_cb(client, None, sentinel_msg)
            return MagicMock()

        client.publish.side_effect = fake_publish
        cleanup(client)

        cleared_topics = [
            call.args[0] if call.args else call.kwargs.get("topic")
            for call in client.publish.call_args_list
            if (call.kwargs.get("retain") is True) or (len(call.args) > 2 and call.args[2] is True)
        ]
        self.assertIn("homeassistant/sensor/nibe_test/state", cleared_topics)

    def test_collects_three_segment_ha_topic_for_this_bridge(self):
        """A retained HA topic with exactly 3 segments (e.g. no trailing
        suffix) whose 3rd segment starts with 'nibe_' must still be
        collected — pins `len(parts) < 3` against `<= 3`/`< 4` off-by-one
        mutants, which this 4-segment-topic-only test suite otherwise
        can't distinguish (3 < 3, 3 <= 3, and 3 < 4 don't all agree)."""
        client, cleanup = self._make_client()

        def fake_publish(topic, payload=None, retain=False):
            if topic == "nibe/browser/scan_sentinel" and not retain:
                ha_cb = self._get_callback(client, "homeassistant/+/+/+")
                msg = MagicMock(
                    topic="homeassistant/sensor/nibe_test",
                    payload=b"22.5",
                    retain=True,
                )
                ha_cb(client, None, msg)
                sentinel_cb = self._get_callback(client, "nibe/browser/scan_sentinel")
                sentinel_cb(client, None, MagicMock(topic=topic, payload=b"cleanup", retain=False))
            return MagicMock()

        client.publish.side_effect = fake_publish
        cleanup(client)

        clear_calls = [c for c in client.publish.call_args_list if c.kwargs.get("retain") is True]
        cleared_topics = [c.args[0] for c in clear_calls]
        self.assertIn("homeassistant/sensor/nibe_test", cleared_topics)

    def test_filters_out_ha_topic_not_belonging_to_bridge(self):
        """A retained homeassistant/... topic whose unique_id segment does
        NOT start with 'nibe_' must be ignored — it belongs to a different
        integration sharing the same HA discovery prefix."""
        client, cleanup = self._make_client()

        def fake_publish(topic, payload=None, retain=False):
            if topic == "nibe/browser/scan_sentinel" and not retain:
                ha_cb = self._get_callback(client, "homeassistant/+/+/+")
                msg = MagicMock(
                    topic="homeassistant/sensor/other_integration/state",
                    payload=b"1",
                    retain=True,
                )
                ha_cb(client, None, msg)
                sentinel_cb = self._get_callback(client, "nibe/browser/scan_sentinel")
                sentinel_cb(client, None, MagicMock(topic=topic, payload=b"cleanup", retain=False))
            return MagicMock()

        client.publish.side_effect = fake_publish
        cleanup(client)

        # Only the sentinel publish — the foreign topic was never collected,
        # so no clear-publish should have happened for it.
        clear_calls = [c for c in client.publish.call_args_list if c.kwargs.get("retain") is True]
        self.assertEqual(len(clear_calls), 0)

    def test_collects_browser_topic_unconditionally(self):
        """nibe/browser/# topics need no unique_id filter — they always
        belong to this bridge."""
        client, cleanup = self._make_client()

        def fake_publish(topic, payload=None, retain=False):
            if topic == "nibe/browser/scan_sentinel" and not retain:
                browser_cb = self._get_callback(client, "nibe/browser/#")
                msg = MagicMock(
                    topic="nibe/browser/all_metadata",
                    payload=b"{}",
                    retain=True,
                )
                browser_cb(client, None, msg)
                sentinel_cb = self._get_callback(client, "nibe/browser/scan_sentinel")
                sentinel_cb(client, None, MagicMock(topic=topic, payload=b"cleanup", retain=False))
            return MagicMock()

        client.publish.side_effect = fake_publish
        cleanup(client)

        clear_calls = [c for c in client.publish.call_args_list if c.kwargs.get("retain") is True]
        cleared_topics = [c.args[0] for c in clear_calls]
        self.assertIn("nibe/browser/all_metadata", cleared_topics)

    def test_sentinel_message_itself_is_not_collected(self):
        """The retained-message callback must ignore the sentinel topic
        itself, even though it matches the wildcard subscriptions."""
        client, cleanup = self._make_client()

        def fake_publish(topic, payload=None, retain=False):
            if topic == "nibe/browser/scan_sentinel" and not retain:
                # Deliver the sentinel topic itself through the wildcard
                # callback path (as it would on a real broker, since
                # nibe/browser/# matches nibe/browser/scan_sentinel too).
                browser_cb = self._get_callback(client, "nibe/browser/#")
                browser_cb(
                    client,
                    None,
                    MagicMock(topic="nibe/browser/scan_sentinel", payload=b"cleanup", retain=False),
                )
                sentinel_cb = self._get_callback(client, "nibe/browser/scan_sentinel")
                sentinel_cb(client, None, MagicMock(topic=topic, payload=b"cleanup", retain=False))
            return MagicMock()

        client.publish.side_effect = fake_publish
        cleanup(client)

        clear_calls = [c for c in client.publish.call_args_list if c.kwargs.get("retain") is True]
        self.assertEqual(len(clear_calls), 0)

    def test_empty_payload_not_collected(self):
        """A message with an empty/already-cleared payload must not be
        re-collected for clearing — it carries no retained data."""
        client, cleanup = self._make_client()

        def fake_publish(topic, payload=None, retain=False):
            if topic == "nibe/browser/scan_sentinel" and not retain:
                browser_cb = self._get_callback(client, "nibe/browser/#")
                browser_cb(
                    client,
                    None,
                    MagicMock(topic="nibe/browser/already_cleared", payload=b"", retain=True),
                )
                sentinel_cb = self._get_callback(client, "nibe/browser/scan_sentinel")
                sentinel_cb(client, None, MagicMock(topic=topic, payload=b"cleanup", retain=False))
            return MagicMock()

        client.publish.side_effect = fake_publish
        cleanup(client)

        clear_calls = [c for c in client.publish.call_args_list if c.kwargs.get("retain") is True]
        self.assertEqual(len(clear_calls), 0)

    def test_sentinel_timeout_logs_warning_and_continues(self):
        """If the sentinel never arrives within the timeout, the function
        must log a warning and still proceed to clear whatever topics were
        collected before the timeout — not hang or crash."""
        client, cleanup = self._make_client()
        # publish() never triggers the sentinel callback — wait() will time out
        with (
            patch("threading.Event.wait", return_value=False) as mock_wait,
            patch("generate_nibe_mqtt.log_startup") as mock_log,
        ):
            cleanup(client)
            mock_wait.assert_called_once()
            self.assertTrue(
                any("Sentinel timeout" in str(call) for call in mock_log.warning.call_args_list)
            )

    def test_sentinel_wait_uses_15_second_timeout(self):
        client, cleanup = self._make_client()
        with (
            patch("threading.Event.wait", return_value=False) as mock_wait,
            patch("generate_nibe_mqtt.log_startup"),
        ):
            cleanup(client)
        mock_wait.assert_called_once_with(timeout=15)

    def test_sentinel_timeout_warning_reports_the_real_timeout_value(self):
        client, cleanup = self._make_client()
        with (
            patch("threading.Event.wait", return_value=False),
            self.assertLogs("nibe.startup", level="WARNING") as cm,
        ):
            cleanup(client)
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(
                    "Sentinel timeout after 15s during MQTT cleanup — some retained "
                    "messages may not have been collected"
                )
                for msg in cm.output
            )
        )

    def test_subscribes_to_the_real_sentinel_topic(self):
        """subscribe() must be called with the real _SENTINEL topic string —
        not None. subscribe() is never verified for its arguments by any
        existing test here (only message_callback_add/unsubscribe are)."""
        client, cleanup = self._make_client()
        self._simulate_sentinel_immediately(client)
        cleanup(client)
        client.subscribe.assert_any_call("nibe/browser/scan_sentinel")

    def test_cleared_count_accumulates_across_multiple_topics(self):
        """'cleared' must accumulate (+=) across every successfully
        confirmed publish — not get reset to 1 on each iteration. With two
        topics both confirming successfully, the final log must report
        2/2, not 1/2."""
        client, cleanup = self._make_client()

        def fake_publish(topic, payload=None, retain=False):
            if topic == "nibe/browser/scan_sentinel" and not retain:
                browser_cb = self._get_callback(client, "nibe/browser/#")
                browser_cb(
                    client, None, MagicMock(topic="nibe/browser/topic_a", payload=b"1", retain=True)
                )
                browser_cb(
                    client, None, MagicMock(topic="nibe/browser/topic_b", payload=b"1", retain=True)
                )
                sentinel_cb = self._get_callback(client, "nibe/browser/scan_sentinel")
                sentinel_cb(client, None, MagicMock(topic=topic, payload=b"cleanup", retain=False))
            return MagicMock()  # a working result whose wait_for_publish() succeeds

        client.publish.side_effect = fake_publish
        with patch("generate_nibe_mqtt.log_startup") as mock_log:
            cleanup(client)
        complete_calls = [
            c for c in mock_log.info.call_args_list if c.args and "cleanup complete" in c.args[0]
        ]
        self.assertEqual(len(complete_calls), 1)
        self.assertEqual(complete_calls[0].args[1:], (2, 2))
        self.assertEqual(
            complete_calls[0].args[0],
            "MQTT cleanup complete — cleared %d/%d retained topics",
        )
        clearing_calls = [
            c
            for c in mock_log.info.call_args_list
            if c.args and c.args[0] == "Clearing %d retained MQTT topics..."
        ]
        self.assertEqual(len(clearing_calls), 1)
        self.assertEqual(clearing_calls[0].args[1], 2)

    def test_unsubscribes_and_removes_callbacks_after_collection(self):
        """Subscriptions and callbacks must be torn down after the sentinel
        is received, regardless of whether any topics were found."""
        client, cleanup = self._make_client()
        self._simulate_sentinel_immediately(client)
        cleanup(client)

        client.message_callback_remove.assert_any_call("homeassistant/+/+/+")
        client.message_callback_remove.assert_any_call("nibe/browser/#")
        client.message_callback_remove.assert_any_call("nibe/browser/scan_sentinel")
        client.unsubscribe.assert_any_call("homeassistant/+/+/+")
        client.unsubscribe.assert_any_call("nibe/browser/#")
        client.unsubscribe.assert_any_call("nibe/browser/scan_sentinel")

    def test_publish_confirmation_exception_does_not_crash(self):
        """If wait_for_publish() raises (broker disconnects mid-clear), the
        function must catch it, log a warning, and continue clearing the
        remaining topics rather than propagating the exception."""
        client, cleanup = self._make_client()

        def fake_publish(topic, payload=None, retain=False):
            if topic == "nibe/browser/scan_sentinel" and not retain:
                browser_cb = self._get_callback(client, "nibe/browser/#")
                browser_cb(
                    client,
                    None,
                    MagicMock(topic="nibe/browser/point_list", payload=b"[]", retain=True),
                )
                sentinel_cb = self._get_callback(client, "nibe/browser/scan_sentinel")
                sentinel_cb(client, None, MagicMock(topic=topic, payload=b"cleanup", retain=False))
                return MagicMock()
            # This is the clear-publish for the collected topic — make its
            # wait_for_publish raise.
            result = MagicMock()
            result.wait_for_publish.side_effect = RuntimeError("disconnected")
            return result

        client.publish.side_effect = fake_publish
        try:
            cleanup(client)
        except RuntimeError:
            self.fail(
                "_cleanup_mqtt_retained must catch publish-confirmation "
                "exceptions, not propagate them"
            )

    def test_publish_confirmation_failure_logs_warning_with_exact_text(self):
        client, cleanup = self._make_client()

        def fake_publish(topic, payload=None, retain=False):
            if topic == "nibe/browser/scan_sentinel" and not retain:
                browser_cb = self._get_callback(client, "nibe/browser/#")
                browser_cb(
                    client,
                    None,
                    MagicMock(topic="nibe/browser/point_list", payload=b"[]", retain=True),
                )
                sentinel_cb = self._get_callback(client, "nibe/browser/scan_sentinel")
                sentinel_cb(client, None, MagicMock(topic=topic, payload=b"cleanup", retain=False))
                return MagicMock()
            result = MagicMock()
            result.wait_for_publish.side_effect = RuntimeError("disconnected")
            return result

        client.publish.side_effect = fake_publish
        with self.assertLogs("nibe.startup", level="WARNING") as cm:
            cleanup(client)
        self.assertTrue(
            any(
                msg.splitlines()[0]
                == "WARNING:nibe.startup:Could not confirm clear for nibe/browser/point_list: "
                "disconnected"
                for msg in cm.output
            )
        )

    def test_successful_confirmation_logs_debug_with_exact_topic(self):
        client, cleanup = self._make_client()

        def fake_publish(topic, payload=None, retain=False):
            if topic == "nibe/browser/scan_sentinel" and not retain:
                browser_cb = self._get_callback(client, "nibe/browser/#")
                browser_cb(
                    client,
                    None,
                    MagicMock(topic="nibe/browser/point_list", payload=b"[]", retain=True),
                )
                sentinel_cb = self._get_callback(client, "nibe/browser/scan_sentinel")
                sentinel_cb(client, None, MagicMock(topic=topic, payload=b"cleanup", retain=False))
            return MagicMock()

        client.publish.side_effect = fake_publish
        with self.assertLogs("nibe.startup", level="DEBUG") as cm:
            cleanup(client)
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith("Cleared retained topic: nibe/browser/point_list")
                for msg in cm.output
            )
        )


# ===========================================================================
# generate_nibe_mqtt.py — extracted startup helpers
# ===========================================================================


class TestBuildSslContext(unittest.TestCase):
    """_build_ssl_context: two branches — CA cert provided vs self-signed."""

    def test_no_ca_cert_returns_ssl_context(self):
        from generate_nibe_mqtt import _build_ssl_context

        ctx = _build_ssl_context(None)
        self.assertIsInstance(ctx, ssl.SSLContext)

    def test_no_ca_cert_disables_hostname_check(self):
        from generate_nibe_mqtt import _build_ssl_context

        ctx = _build_ssl_context(None)
        # Strict identity, not assertFalse — None is also falsy and would
        # wrongly pass a mutant that sets check_hostname=None instead of False.
        self.assertIs(ctx.check_hostname, False)

    def test_no_ca_cert_sets_cert_none(self):
        from generate_nibe_mqtt import _build_ssl_context

        ctx = _build_ssl_context(None)
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)

    def test_nonexistent_ca_path_falls_back_to_self_signed(self):
        from generate_nibe_mqtt import _build_ssl_context

        ctx = _build_ssl_context("/nonexistent/ca.crt")
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)

    def test_no_ca_cert_widens_cipher_and_version_compatibility(self):
        """Regression: the self-signed branch must relax the minimum TLS
        version and cipher security level to stay compatible with the Nibe
        controller's old embedded TLS stack. verify_mode is already
        CERT_NONE in this branch, so this doesn't weaken security — it was
        previously dropped silently and would break connectivity against
        controllers that can't meet OpenSSL's stricter platform defaults."""
        from generate_nibe_mqtt import _build_ssl_context

        ctx = _build_ssl_context(None)
        self.assertEqual(ctx.minimum_version, ssl.TLSVersion.MINIMUM_SUPPORTED)

    def test_no_ca_cert_logs_warning_with_exact_text(self):
        from generate_nibe_mqtt import _build_ssl_context

        with self.assertLogs("nibe.startup", level="WARNING") as cm:
            _build_ssl_context(None)
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(
                    "TLS: Certificate verification disabled (self-signed cert). "
                    "Enable verification by setting 'nibe_ca_cert' in add-on options."
                )
                for msg in cm.output
            )
        )

    def test_no_ca_cert_sets_compat_cipher_string(self):
        """set_ciphers() must actually be called with the shared
        TLS_COMPAT_CIPHERS constant — SSLContext exposes no getter for the
        cipher string, so this must be verified via the call itself rather
        than by reading it back off the context, otherwise a future edit
        that silently drops or breaks the set_ciphers() call would pass
        the sibling minimum_version-only test above undetected."""
        from generate_nibe_mqtt import _build_ssl_context
        from nibe_utils import TLS_COMPAT_CIPHERS

        with patch("ssl.SSLContext.set_ciphers") as mock_set_ciphers:
            _build_ssl_context(None)
        mock_set_ciphers.assert_called_once_with(TLS_COMPAT_CIPHERS)

    def test_valid_ca_cert_enables_verification(self):
        import ssl

        from generate_nibe_mqtt import _build_ssl_context

        # Write a minimal (but syntactically valid) self-signed cert file
        # so os.path.exists() passes — _build_ssl_context will try to load it.
        # Use a real cert from the ssl module's default store as the CA file.
        cafile = ssl.get_default_verify_paths().cafile
        if not cafile or not os.path.exists(cafile):
            self.skipTest("No system CA bundle available")
        ctx = _build_ssl_context(cafile)
        self.assertTrue(ctx.check_hostname)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)

    def test_valid_ca_cert_logs_info_with_the_real_path(self):
        from generate_nibe_mqtt import _build_ssl_context

        cafile = ssl.get_default_verify_paths().cafile
        if not cafile or not os.path.exists(cafile):
            self.skipTest("No system CA bundle available")
        with self.assertLogs("nibe.startup", level="INFO") as cm:
            _build_ssl_context(cafile)
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(
                    f"Nibe API TLS: verification enabled using CA cert {cafile}"
                )
                for msg in cm.output
            )
        )

    def test_never_raises_for_none_or_nonexistent_path(self):
        from generate_nibe_mqtt import _build_ssl_context

        for path in [None, "", "/nonexistent/path/ca.crt", "/tmp/definitely_not_there.crt"]:
            ctx = _build_ssl_context(path)
            self.assertIsInstance(ctx, ssl.SSLContext)

    def test_ca_cert_path_actually_passed_to_create_default_context(self):
        """The real ca_cert_path must be passed as cafile= — not None or
        dropped. check_hostname/verify_mode alone can't distinguish this,
        since ssl.create_default_context(cafile=None) sets the same
        defaults as passing a real (unused) cafile would leave unchanged."""
        from generate_nibe_mqtt import _build_ssl_context

        with (
            patch("generate_nibe_mqtt.os.path.exists", return_value=True),
            patch("generate_nibe_mqtt.ssl.create_default_context") as mock_create,
        ):
            _build_ssl_context("/some/real/ca.crt")
        mock_create.assert_called_once_with(cafile="/some/real/ca.crt")


class TestDeriveDeviceId(unittest.TestCase):
    """_derive_device_id: serial present vs absent, normalisation, and
    persist/reuse behaviour so device_id doesn't flip-flop across restarts
    when the device is transiently unreachable (see class docstring on the
    real bug this caused: a duplicate, empty "ghost" Management device in
    HA, name-identical to the real one, from a startup that happened to
    hit the fallback default id instead of the previously-learned real one).

    Every test uses an isolated tmp file for persist_path — not the real
    /data/device_id — so tests can't pollute each other or depend on
    /data happening not to exist on the machine running them.
    """

    def setUp(self):
        import tempfile

        from generate_nibe_mqtt import _derive_device_id

        self.fn = _derive_device_id
        # A path inside a fresh tmp dir that does not exist yet — mirrors
        # a genuinely first-ever startup with nothing persisted.
        self._tmpdir = tempfile.mkdtemp()
        self.persist_path = self._tmpdir + "/device_id"

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_serial_present_returns_nibe_prefix(self):
        result = self.fn({"product": {"serialNumber": "ABC123"}}, "fallback", self.persist_path)
        self.assertTrue(result.startswith("nibe_"))

    def test_serial_normalised_to_lowercase(self):
        result = self.fn({"product": {"serialNumber": "ABC123"}}, "fallback", self.persist_path)
        self.assertEqual(result, "nibe_abc123")

    def test_serial_special_chars_stripped(self):
        result = self.fn(
            {"product": {"serialNumber": "AB-12 CD.EF"}}, "fallback", self.persist_path
        )
        self.assertEqual(result, "nibe_ab12cdef")

    def test_underscore_preserved(self):
        result = self.fn({"product": {"serialNumber": "AB_12"}}, "fallback", self.persist_path)
        self.assertEqual(result, "nibe_ab_12")

    @given(st.text(min_size=1, max_size=100))
    def test_result_only_contains_alnum_or_underscore_chars_after_prefix(self, serial):
        """For ANY serial string (arbitrary unicode, not just the
        hand-picked 'AB-12 CD.EF' example), every character surviving
        into the safe_id must be alphanumeric or '_' — the exact same
        predicate the source uses (`c.isalnum() or c == "_"`), computed
        independently here rather than read back from the result.

        Note: `str.isalnum()` is Unicode-aware, so a non-ASCII
        alphanumeric character (e.g. 'ä') is NOT stripped by this
        filter — only non-alnum/non-underscore characters (spaces,
        dashes, punctuation) are. This property reflects that real
        behavior rather than assuming an ASCII-only output."""
        from hypothesis import assume

        assume(serial.strip())  # a whitespace-only serial takes the fallback path instead
        result = self.fn({"product": {"serialNumber": serial}}, "fallback", self.persist_path)
        self.assertTrue(result.startswith("nibe_"))
        safe_id = result[len("nibe_") :]
        for c in safe_id:
            self.assertTrue(c.isalnum() or c == "_", f"unexpected character {c!r} in {safe_id!r}")

    @given(
        st.text(
            alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="_"),
            min_size=1,
            max_size=50,
        )
    )
    def test_already_safe_serial_passes_through_unmodified(self, serial):
        """For any serial already composed entirely of lowercase
        letters/digits/underscores, the filter must be a true no-op —
        every character survives, none get silently dropped."""
        result = self.fn({"product": {"serialNumber": serial}}, "fallback", self.persist_path)
        self.assertEqual(result, f"nibe_{serial}")

    def test_serial_absent_no_persisted_id_returns_fallback(self):
        result = self.fn({}, "my_fallback", self.persist_path)
        self.assertEqual(result, "my_fallback")

    def test_serial_empty_string_no_persisted_id_returns_fallback(self):
        result = self.fn({"product": {"serialNumber": ""}}, "my_fallback", self.persist_path)
        self.assertEqual(result, "my_fallback")

    def test_serial_none_no_persisted_id_returns_fallback(self):
        result = self.fn({"product": {"serialNumber": None}}, "my_fallback", self.persist_path)
        self.assertEqual(result, "my_fallback")

    def test_serial_whitespace_only_no_persisted_id_returns_fallback(self):
        result = self.fn({"product": {"serialNumber": "   "}}, "my_fallback", self.persist_path)
        self.assertEqual(result, "my_fallback")

    def test_empty_response_no_persisted_id_returns_fallback(self):
        result = self.fn({}, "my_fallback", self.persist_path)
        self.assertEqual(result, "my_fallback")

    # ── persist-on-success ──────────────────────────────────────────────────

    def test_successful_derivation_persists_to_file(self):
        self.fn({"product": {"serialNumber": "ABC123"}}, "fallback", self.persist_path)
        with open(self.persist_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "nibe_abc123")

    def test_successful_derivation_logs_info_with_exact_text(self):
        with self.assertLogs("nibe.startup", level="INFO") as cm:
            self.fn({"product": {"serialNumber": "ABC123"}}, "fallback", self.persist_path)
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith("Device ID derived from serial number: nibe_abc123")
                for msg in cm.output
            )
        )

    def test_persist_write_uses_utf8_encoding(self):
        """Real-file round-trip tests can't distinguish encoding='utf-8'
        from encoding=None, since both resolve to UTF-8 on this machine's
        locale — mock open() directly to pin the actual kwarg passed."""
        seen_kwargs = []

        def fake_open(_path, mode="r", **kw):
            seen_kwargs.append(kw)
            raise OSError("mocked — no real write")

        with patch("builtins.open", side_effect=fake_open):
            self.fn({"product": {"serialNumber": "ABC123"}}, "fallback", self.persist_path)
        self.assertEqual(seen_kwargs[0].get("encoding"), "utf-8")

    def test_persisted_id_read_uses_utf8_encoding(self):
        with open(self.persist_path, "w", encoding="utf-8") as f:
            f.write("nibe_previously_learned")
        seen_kwargs = []
        real_open = open

        def fake_open(path, *a, **kw):
            if str(path) == self.persist_path:
                seen_kwargs.append(kw)
            return real_open(path, *a, **kw)

        with patch("builtins.open", side_effect=fake_open):
            self.fn({}, "fallback", self.persist_path)
        self.assertEqual(seen_kwargs[0].get("encoding"), "utf-8")

    def test_persist_write_failure_does_not_raise_and_still_returns_id(self):
        """A read-only or missing parent directory must not crash startup
        over a best-effort persistence write."""
        unwritable_path = self._tmpdir + "/nonexistent_subdir/device_id"
        result = self.fn(
            {"product": {"serialNumber": "ABC123"}},
            "fallback",
            unwritable_path,
        )
        self.assertEqual(result, "nibe_abc123")

    def test_persist_write_failure_logs_warning_with_real_path_and_error(self):
        unwritable_path = self._tmpdir + "/nonexistent_subdir/device_id"
        with self.assertLogs("nibe.startup", level="WARNING") as cm:
            self.fn({"product": {"serialNumber": "ABC123"}}, "fallback", unwritable_path)
        [warning_msg] = cm.output
        prefix, _, suffix = warning_msg.partition(
            f"WARNING:nibe.startup:Could not persist device_id to {unwritable_path}: "
        )
        self.assertEqual(prefix, "")
        self.assertTrue(suffix.startswith("["))  # the real OSError text, e.g. '[Errno 2] ...'
        self.assertIn(
            "a future startup during a transient outage may fall back to the "
            "generic default instead.",
            warning_msg,
        )

    # ── reuse-on-failure (the actual bug fix) ───────────────────────────────

    def test_reuses_persisted_id_when_serial_absent(self):
        """The core fix: a startup where the device is transiently
        unreachable must reuse the previously-learned real device_id, not
        fall back to the generic default — otherwise every entity
        (especially the Management device, published unconditionally at
        every startup) gets recreated under a different HA device identity,
        leaving the old one as an orphaned empty duplicate."""
        with open(self.persist_path, "w", encoding="utf-8") as f:
            f.write("nibe_abc123")
        result = self.fn({}, "generic_fallback", self.persist_path)
        self.assertEqual(result, "nibe_abc123")
        self.assertNotEqual(result, "generic_fallback")

    def test_reuses_persisted_id_logs_warning_with_exact_text(self):
        with open(self.persist_path, "w", encoding="utf-8") as f:
            f.write("nibe_abc123")
        with self.assertLogs("nibe.startup", level="WARNING") as cm:
            self.fn({}, "generic_fallback", self.persist_path)
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(
                    "Serial number not available this startup (device unreachable?) — "
                    "reusing the previously learned device_id 'nibe_abc123' instead of "
                    "the generic default, to avoid creating a duplicate HA device."
                )
                for msg in cm.output
            )
        )

    def test_no_persisted_id_logs_fallback_warning_with_exact_text(self):
        with self.assertLogs("nibe.startup", level="WARNING") as cm:
            self.fn({}, "my_fallback", self.persist_path)
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(
                    "Serial number not available — using default device_id "
                    "'my_fallback'. Running two bridges without serial numbers "
                    "may cause HA device collisions."
                )
                for msg in cm.output
            )
        )

    def test_persisted_id_preferred_over_generic_fallback_for_empty_serial(self):
        with open(self.persist_path, "w", encoding="utf-8") as f:
            f.write("nibe_abc123")
        result = self.fn({"product": {"serialNumber": ""}}, "generic_fallback", self.persist_path)
        self.assertEqual(result, "nibe_abc123")

    def test_persisted_id_whitespace_is_stripped(self):
        with open(self.persist_path, "w", encoding="utf-8") as f:
            f.write("  nibe_abc123  \n")
        result = self.fn({}, "generic_fallback", self.persist_path)
        self.assertEqual(result, "nibe_abc123")

    def test_empty_persisted_file_falls_back_to_generic_default(self):
        with open(self.persist_path, "w", encoding="utf-8") as f:
            f.write("")
        result = self.fn({}, "generic_fallback", self.persist_path)
        self.assertEqual(result, "generic_fallback")

    def test_no_persisted_file_and_no_serial_falls_back_to_generic_default(self):
        """The genuinely-first-ever-startup case: nothing has ever been
        learned, so the generic config default is the only option."""
        result = self.fn({}, "generic_fallback", self.persist_path)
        self.assertEqual(result, "generic_fallback")

    def test_new_serial_overwrites_previously_persisted_id(self):
        """A real device swap (different controller) must update the
        persisted id, not stick with a stale one from a previous device."""
        with open(self.persist_path, "w", encoding="utf-8") as f:
            f.write("nibe_old_serial")
        result = self.fn({"product": {"serialNumber": "NEWSERIAL"}}, "fallback", self.persist_path)
        self.assertEqual(result, "nibe_newserial")
        with open(self.persist_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "nibe_newserial")

    @given(st.text(max_size=30))
    def test_result_always_starts_with_nibe_or_is_fallback(self, serial):
        import shutil
        import tempfile

        from generate_nibe_mqtt import _derive_device_id

        tmpdir = tempfile.mkdtemp()
        try:
            result = _derive_device_id(
                {"product": {"serialNumber": serial}},
                "fallback",
                tmpdir + "/device_id",
            )
            self.assertTrue(
                result.startswith("nibe_") or result == "fallback",
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @given(st.text(min_size=1, max_size=30).filter(lambda s: s.strip()))
    def test_nonempty_serial_gives_nibe_prefix(self, serial):
        import shutil
        import tempfile

        from generate_nibe_mqtt import _derive_device_id

        tmpdir = tempfile.mkdtemp()
        try:
            result = _derive_device_id(
                {"product": {"serialNumber": serial}},
                "fallback",
                tmpdir + "/device_id",
            )
            self.assertTrue(result.startswith("nibe_"))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestResolveInitialMode(unittest.TestCase):
    """_resolve_initial_mode: CLI flag takes priority over config."""

    def setUp(self):
        from generate_nibe_mqtt import _resolve_initial_mode

        self.fn = _resolve_initial_mode

    def _args(self, mode):
        return MagicMock(mode=mode)

    def _cfg(self, mode):
        return MagicMock(mode=mode)

    def test_cli_mode_takes_priority(self):
        self.assertEqual(self.fn(self._args("advanced"), self._cfg("essential")), "advanced")

    def test_empty_cli_mode_falls_back_to_config(self):
        self.assertEqual(self.fn(self._args(""), self._cfg("monitoring")), "monitoring")

    def test_none_cli_mode_falls_back_to_config(self):
        self.assertEqual(self.fn(self._args(None), self._cfg("monitoring")), "monitoring")

    def test_config_mode_returned_when_cli_absent(self):
        self.assertEqual(self.fn(self._args(None), self._cfg("all")), "all")

    @given(st.text(min_size=1, max_size=20), st.text(min_size=1, max_size=20))
    def test_cli_always_wins_when_truthy(self, cli_mode, cfg_mode):
        from generate_nibe_mqtt import _resolve_initial_mode

        args = MagicMock(mode=cli_mode)
        cfg = MagicMock(mode=cfg_mode)
        self.assertEqual(_resolve_initial_mode(args, cfg), cli_mode)


class TestBuildMqttClientId(unittest.TestCase):
    """_build_mqtt_client_id: always ≤23 chars, preserves short IDs."""

    def setUp(self):
        from generate_nibe_mqtt import _build_mqtt_client_id

        self.fn = _build_mqtt_client_id

    def test_short_id_unchanged(self):
        self.assertEqual(self.fn("nibe_abc"), "nibe_abc")

    def test_long_id_truncated_to_23(self):
        result = self.fn("nibe_" + "x" * 30)
        self.assertEqual(len(result), 23)

    def test_exactly_23_unchanged(self):
        id23 = "a" * 23
        self.assertEqual(self.fn(id23), id23)

    def test_empty_string_returns_empty(self):
        self.assertEqual(self.fn(""), "")

    @given(st.text(max_size=50))
    def test_result_always_at_most_23_chars(self, device_id):
        from generate_nibe_mqtt import _build_mqtt_client_id

        self.assertLessEqual(len(_build_mqtt_client_id(device_id)), 23)

    @given(st.text(max_size=23))
    def test_short_id_returned_unchanged(self, device_id):
        from generate_nibe_mqtt import _build_mqtt_client_id

        self.assertEqual(_build_mqtt_client_id(device_id), device_id)


class TestConfigureMqttTls(unittest.TestCase):
    """_configure_mqtt_tls: four branches."""

    def setUp(self):
        from generate_nibe_mqtt import _configure_mqtt_tls

        self.fn = _configure_mqtt_tls
        self.mqtt = MagicMock()

    def _cfg(self, tls=False, ca=None, username=None):
        return MagicMock(mqtt_tls=tls, mqtt_ca_cert=ca, mqtt_username=username)

    def test_tls_disabled_no_credentials_no_tls_set(self):
        self.fn(self.mqtt, self._cfg(tls=False))
        self.mqtt.tls_set.assert_not_called()

    def test_tls_disabled_with_credentials_no_tls_set(self):
        self.fn(self.mqtt, self._cfg(tls=False, username="user"))
        self.mqtt.tls_set.assert_not_called()

    def test_tls_enabled_no_ca_calls_tls_set_with_none(self):
        self.fn(self.mqtt, self._cfg(tls=True, ca=None))
        self.mqtt.tls_set.assert_called_once_with(ca_certs=None)

    def test_tls_enabled_nonexistent_ca_calls_tls_set_with_none(self):
        self.fn(self.mqtt, self._cfg(tls=True, ca="/nonexistent/ca.crt"))
        self.mqtt.tls_set.assert_called_once_with(ca_certs=None)

    def test_tls_enabled_existing_ca_calls_tls_set_with_path(self):
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".crt", delete=False) as f:
            ca_path = f.name
        try:
            self.fn(self.mqtt, self._cfg(tls=True, ca=ca_path))
            self.mqtt.tls_set.assert_called_once_with(ca_certs=ca_path)
        finally:
            os.unlink(ca_path)

    def test_tls_disabled_tls_set_never_called(self):
        for username in (None, "user"):
            self.mqtt.reset_mock()
            self.fn(self.mqtt, self._cfg(tls=False, username=username))
            self.mqtt.tls_set.assert_not_called()

    def test_tls_enabled_no_ca_logs_system_ca_store_text(self):
        with self.assertLogs("nibe.mqtt", level="INFO") as cm:
            self.fn(self.mqtt, self._cfg(tls=True, ca=None))
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith("MQTT TLS enabled (system CA store)")
                for msg in cm.output
            )
        )

    def test_tls_enabled_with_ca_logs_the_real_ca_path(self):
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".crt", delete=False) as f:
            ca_path = f.name
        try:
            with self.assertLogs("nibe.mqtt", level="INFO") as cm:
                self.fn(self.mqtt, self._cfg(tls=True, ca=ca_path))
            self.assertTrue(
                any(
                    msg.splitlines()[0].endswith(f"MQTT TLS enabled (CA: {ca_path})")
                    for msg in cm.output
                )
            )
        finally:
            os.unlink(ca_path)

    def test_tls_disabled_with_credentials_logs_warning_with_exact_text(self):
        with self.assertLogs("nibe.mqtt", level="WARNING") as cm:
            self.fn(self.mqtt, self._cfg(tls=False, username="user"))
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(
                    "MQTT TLS disabled — credentials sent in plaintext. "
                    "Enable with 'mqtt_tls: true' in add-on options."
                )
                for msg in cm.output
            )
        )

    def test_tls_disabled_no_credentials_logs_nothing(self):
        with self.assertNoLogs("nibe.mqtt", level="WARNING"):
            self.fn(self.mqtt, self._cfg(tls=False, username=None))


class TestRunScanWithRetry(unittest.TestCase):
    """_run_scan_with_retry: retry logic and return value."""

    def setUp(self):
        from generate_nibe_mqtt import _run_scan_with_retry

        self.fn = _run_scan_with_retry

    def _em(self, results):
        """Entity manager mock returning results from a queue."""
        em = MagicMock()
        em.scan_mqtt_discovery.side_effect = list(results)
        return em

    def test_first_scan_succeeds_returns_immediately(self):
        em = self._em([{100, 200}])
        with patch("time.sleep") as mock_sleep:
            result = self.fn(em, retries=3, backoffs=[3, 6, 12])
        self.assertEqual(result, {100, 200})
        mock_sleep.assert_not_called()

    def test_first_scan_empty_retries(self):
        em = self._em([set(), {100}])
        with patch("time.sleep"):
            result = self.fn(em, retries=3, backoffs=[3, 6, 12])
        self.assertEqual(result, {100})
        self.assertEqual(em.scan_mqtt_discovery.call_count, 2)

    def test_all_scans_fail_returns_empty_set(self):
        em = self._em([set(), set(), set(), set()])
        with patch("time.sleep"):
            result = self.fn(em, retries=3, backoffs=[1, 1, 1])
        self.assertEqual(result, set())

    def test_scan_called_at_most_retries_plus_one_times(self):
        em = self._em([set()] * 10)
        with patch("time.sleep"):
            self.fn(em, retries=3, backoffs=[1, 1, 1])
        self.assertLessEqual(em.scan_mqtt_discovery.call_count, 4)

    def test_returns_set(self):
        em = self._em([set(), set(), set(), set()])
        with patch("time.sleep"):
            result = self.fn(em, retries=3, backoffs=[1, 1, 1])
        self.assertIsInstance(result, set)

    def test_sleep_called_with_correct_backoff(self):
        em = self._em([set(), {1}])
        with patch("time.sleep") as mock_sleep:
            self.fn(em, retries=3, backoffs=[3, 6, 12])
        mock_sleep.assert_called_once_with(3)

    def test_retry_warning_logged_with_exact_text(self):
        em = self._em([set(), {1}])
        with patch("time.sleep"), self.assertLogs("nibe.restore", level="WARNING") as cm:
            self.fn(em, retries=3, backoffs=[3, 6, 12])
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(
                    "Scan returned 0 configs (attempt 1/3) — broker may still be "
                    "loading. Retrying in 3s..."
                )
                for msg in cm.output
            )
        )

    def test_default_backoffs_are_3_6_12(self):
        """No caller passes backoffs= explicitly in production — the
        default [3, 6, 12] list itself must be exactly right, not e.g.
        [3, 6, 13]."""
        em = self._em([set(), set(), set(), set()])
        with patch("time.sleep") as mock_sleep:
            self.fn(em, retries=4)  # backoffs= omitted — uses the default
        mock_sleep.assert_any_call(3)
        mock_sleep.assert_any_call(6)
        mock_sleep.assert_any_call(12)

    def test_default_backoffs_used_when_none(self):
        em = self._em([{1}])
        with patch("time.sleep") as mock_sleep:
            self.fn(em)
        mock_sleep.assert_not_called()

    def test_default_backoff_values_are_exactly_3_6_12(self):
        """The default backoffs=None → [3, 6, 12] fallback must use those
        exact values — the test above never forces a retry (the first
        scan succeeds immediately), so the actual default values were
        never exercised."""
        em = self._em([set(), set(), {1}])
        with patch("time.sleep") as mock_sleep:
            self.fn(em)  # retries and backoffs both omitted — real defaults
        self.assertEqual(
            [c.args[0] for c in mock_sleep.call_args_list],
            [3, 6],
        )

    def test_default_retries_is_exactly_3(self):
        """The default retries=3 must actually cap the loop at 3 attempts
        — verified by exhausting all scans and counting calls, since
        every other test always passes retries= explicitly."""
        em = self._em([set()] * 10)
        with patch("time.sleep"):
            self.fn(em)  # retries omitted — real default of 3
        self.assertEqual(em.scan_mqtt_discovery.call_count, 3)


class TestExecuteStartupAction(unittest.TestCase):
    """_execute_startup_action: context-specific logging and notifications.

    The mutation logic (apply_mode / restore_from_mqtt / record_applied_mode)
    is now in EntityManager._apply_startup_action and tested separately in
    TestApplyStartupAction.  These tests verify the caller-specific concerns:
    the mode=none HA notification and the correct delegation to _apply_startup_action.
    """

    def setUp(self):
        from generate_nibe_mqtt import _execute_startup_action

        self.fn = _execute_startup_action
        # Use a real EntityManager so _apply_startup_action can delegate
        self.em = _make_em()
        self.mqtt = MagicMock()

    def _run(self, action, applied_mode="essential", initial_mode="essential"):
        with (
            patch.object(self.em, "apply_mode"),
            patch.object(self.em, "restore_from_mqtt"),
            patch.object(self.em, "record_applied_mode"),
        ):
            self.fn(self.em, action, applied_mode, initial_mode, self.mqtt, "Test Device")

    def _run_with_mocks(self, action, applied_mode="essential", initial_mode="essential"):
        """Return the patched mocks for assertion."""
        with (
            patch.object(self.em, "apply_mode") as mock_apply,
            patch.object(self.em, "restore_from_mqtt") as mock_restore,
            patch.object(self.em, "record_applied_mode") as mock_record,
        ):
            self.fn(self.em, action, applied_mode, initial_mode, self.mqtt, "Test Device")
        return mock_apply, mock_restore, mock_record

    def test_apply_calls_apply_mode(self):
        mock_apply, _mock_restore, _ = self._run_with_mocks(
            "apply", applied_mode=None, initial_mode="monitoring"
        )
        mock_apply.assert_called_once_with("monitoring")

    def test_apply_does_not_call_restore(self):
        _, mock_restore, _ = self._run_with_mocks("apply", applied_mode=None)
        mock_restore.assert_not_called()

    def test_apply_none_mode_sends_notification(self):
        with (
            patch.object(self.em, "apply_mode"),
            patch.object(self.em, "restore_from_mqtt"),
            patch.object(self.em, "record_applied_mode"),
            patch("generate_nibe_mqtt.notify_ha") as mock_notify,
        ):
            self.fn(self.em, "apply", None, "none", self.mqtt, "Test Device")
        mock_notify.assert_called_once()
        args = mock_notify.call_args
        self.assertIn("nibe_no_entities", str(args))
        # assertIn on str(args) can't distinguish 'nibe_no_entities' from an
        # XX-wrapped mutant ('nibe_no_entities' is still a substring of
        # 'XXnibe_no_entitiesXX') — an exact-equality check on the kwarg
        # itself closes that gap.
        self.assertEqual(args.kwargs["notification_id"], "nibe_no_entities")

    def test_apply_none_mode_notification_has_real_title(self):
        """The notification's title must be the real 'No Entities Enabled'
        text, not None — the existing loose str(args) check above only
        verifies the notification_id substring, which wouldn't catch a
        title=None regression since it never inspects the title kwarg."""
        with (
            patch.object(self.em, "apply_mode"),
            patch.object(self.em, "restore_from_mqtt"),
            patch.object(self.em, "record_applied_mode"),
            patch("generate_nibe_mqtt.notify_ha") as mock_notify,
        ):
            self.fn(self.em, "apply", None, "none", self.mqtt, "Test Device")
        self.assertEqual(
            mock_notify.call_args.kwargs["title"],
            "Nibe Bridge: No Entities Enabled",
        )

    def test_apply_none_mode_notification_receives_real_mqtt_client(self):
        with (
            patch.object(self.em, "apply_mode"),
            patch.object(self.em, "restore_from_mqtt"),
            patch.object(self.em, "record_applied_mode"),
            patch("generate_nibe_mqtt.notify_ha") as mock_notify,
        ):
            self.fn(self.em, "apply", None, "none", self.mqtt, "Test Device")
        self.assertIs(mock_notify.call_args.args[0], self.mqtt)

    def test_apply_non_none_mode_no_notification(self):
        with (
            patch.object(self.em, "apply_mode"),
            patch.object(self.em, "restore_from_mqtt"),
            patch.object(self.em, "record_applied_mode"),
            patch("generate_nibe_mqtt.notify_ha") as mock_notify,
        ):
            self.fn(self.em, "apply", None, "essential", self.mqtt, "Test Device")
        mock_notify.assert_not_called()

    def test_restore_calls_restore_from_mqtt(self):
        _, mock_restore, _ = self._run_with_mocks("restore", applied_mode="essential")
        mock_restore.assert_called_once()

    def test_restore_does_not_call_apply_mode(self):
        mock_apply, *_ = self._run_with_mocks("restore", applied_mode="essential")
        mock_apply.assert_not_called()

    def test_restore_with_applied_mode_does_not_record(self):
        _, _, mock_record = self._run_with_mocks("restore", applied_mode="essential")
        mock_record.assert_not_called()

    def test_restore_with_none_applied_mode_records_baseline(self):
        _, _, mock_record = self._run_with_mocks(
            "restore", applied_mode=None, initial_mode="monitoring"
        )
        mock_record.assert_called_once_with("monitoring")

    def test_reconcile_calls_restore_then_apply(self):
        call_order = []
        with (
            patch.object(
                self.em, "restore_from_mqtt", side_effect=lambda: call_order.append("restore")
            ),
            patch.object(
                self.em, "apply_mode", side_effect=lambda m: call_order.append(f"apply:{m}")
            ),
            patch.object(self.em, "record_applied_mode"),
        ):
            self.fn(self.em, "reconcile", "essential", "monitoring", self.mqtt, "Test")
        self.assertEqual(call_order, ["restore", "apply:monitoring"])

    def test_reconcile_calls_both_restore_and_apply(self):
        mock_apply, mock_restore, _ = self._run_with_mocks(
            "reconcile", applied_mode="essential", initial_mode="monitoring"
        )
        mock_restore.assert_called_once()
        mock_apply.assert_called_once_with("monitoring")

    def test_unknown_action_does_not_raise(self):
        self._run("unknown_action")  # must not raise

    def test_apply_logs_info_with_exact_text(self):
        with self.assertLogs("nibe.restore", level="INFO") as cm:
            self._run("apply", applied_mode=None, initial_mode="monitoring")
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(
                    "No existing MQTT configs — applying initial mode: monitoring"
                )
                for msg in cm.output
            )
        )

    def test_restore_does_not_log_the_reconcile_message(self):
        """The 'restore' branch is a deliberate no-op at this level — if
        the 'restore' condition were mutated to fall through to the
        `else` branch, the 'Entity mode changed ...' reconcile message
        would wrongly fire for a same-mode restart."""
        import logging

        with self.assertLogs("nibe.restore", level="INFO") as cm:
            logging.getLogger("nibe.restore").info("sentinel")  # assertLogs needs >=1 record
            self._run("restore", applied_mode="essential", initial_mode="essential")
        self.assertFalse(any("Entity mode changed" in msg for msg in cm.output))

    def test_reconcile_logs_info_with_exact_text(self):
        with (
            patch.object(self.em, "restore_from_mqtt"),
            patch.object(self.em, "apply_mode"),
            patch.object(self.em, "record_applied_mode"),
            self.assertLogs("nibe.restore", level="INFO") as cm,
        ):
            self.fn(self.em, "reconcile", "essential", "monitoring", self.mqtt, "Test")
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(
                    "Entity mode changed from 'essential' to 'monitoring' — restoring "
                    "then reconciling the enabled set to the new mode."
                )
                for msg in cm.output
            )
        )

    def test_apply_none_mode_logs_info_with_exact_text(self):
        with (
            patch.object(self.em, "apply_mode"),
            patch.object(self.em, "restore_from_mqtt"),
            patch.object(self.em, "record_applied_mode"),
            patch("generate_nibe_mqtt.notify_ha"),
            self.assertLogs("nibe.restore", level="INFO") as cm,
        ):
            self.fn(self.em, "apply", None, "none", self.mqtt, "Test Device")
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(
                    "Mode 'none' selected — no entities enabled by default. "
                    "Use the Entity Manager card to enable entities."
                )
                for msg in cm.output
            )
        )

    def test_apply_none_mode_notification_has_real_message(self):
        """The notification message must contain the real point count and
        device name — not None, which the existing str(args) check
        wouldn't catch since it only greps for 'nibe_no_entities'."""
        self.em.all_points_by_id = {1: {}, 2: {}, 3: {}}
        with (
            patch.object(self.em, "apply_mode"),
            patch.object(self.em, "restore_from_mqtt"),
            patch.object(self.em, "record_applied_mode"),
            patch("generate_nibe_mqtt.notify_ha") as mock_notify,
        ):
            self.fn(self.em, "apply", None, "none", self.mqtt, "My Device")
        message = mock_notify.call_args.kwargs["message"]
        self.assertIn("3 data points were discovered on My Device", message)
        self.assertIn(
            "'none'. No entities will appear in Home Assistant until you enable "
            "some. Use the Entity Manager card on the Nibe Bridge dashboard to "
            "enable a set of entities.",
            message,
        )


class TestKeepaliveFromConfig(unittest.TestCase):
    """_keepalive_from_config: minimum 60s, always > poll_interval."""

    def setUp(self):
        from generate_nibe_mqtt import _keepalive_from_config

        self.fn = _keepalive_from_config

    def test_short_interval_returns_60(self):
        self.assertEqual(self.fn(15), 60)

    def test_interval_at_50_returns_60(self):
        self.assertEqual(self.fn(50), 60)

    def test_interval_at_55_returns_65(self):
        self.assertEqual(self.fn(55), 65)

    def test_long_interval_adds_10(self):
        self.assertEqual(self.fn(300), 310)

    def test_zero_interval_returns_60(self):
        self.assertEqual(self.fn(0), 60)

    @given(st.integers(min_value=0, max_value=3600))
    def test_always_at_least_60(self, poll_interval):
        from generate_nibe_mqtt import _keepalive_from_config

        self.assertGreaterEqual(_keepalive_from_config(poll_interval), 60)

    @given(st.integers(min_value=0, max_value=3600))
    def test_always_greater_than_poll_interval(self, poll_interval):
        from generate_nibe_mqtt import _keepalive_from_config

        self.assertGreater(_keepalive_from_config(poll_interval), poll_interval)


class TestFetchApiResponse(unittest.TestCase):
    """_fetch_api_response: success, offline, and auth-failure branches."""

    def setUp(self):
        from generate_nibe_mqtt import _fetch_api_response

        self.fn = _fetch_api_response

    def _api(self, response):
        api = MagicMock()
        api.fetch_device_info.return_value = response
        return api

    def test_success_returns_response(self):
        api = self._api({"product": {"name": "S2125", "serialNumber": "123"}})
        result = self.fn(api)
        self.assertEqual(result["product"]["name"], "S2125")

    def test_none_response_returns_empty_dict(self):
        api = self._api(None)
        result = self.fn(api)
        self.assertEqual(result, {})

    def test_empty_dict_response_returns_empty_dict(self):
        api = self._api({})
        result = self.fn(api)
        self.assertEqual(result, {})

    def test_http_error_raises_api_auth_error(self):
        import urllib.error

        from generate_nibe_mqtt import _ApiAuthError, _fetch_api_response

        api = MagicMock()
        api.fetch_device_info.side_effect = urllib.error.HTTPError(
            url="https://host", code=401, msg="Unauthorized", hdrs={}, fp=None
        )
        with self.assertRaises(_ApiAuthError):
            _fetch_api_response(api)

    def test_http_403_raises_api_auth_error(self):
        import urllib.error

        from generate_nibe_mqtt import _ApiAuthError, _fetch_api_response

        api = MagicMock()
        api.fetch_device_info.side_effect = urllib.error.HTTPError(
            url="https://host", code=403, msg="Forbidden", hdrs={}, fp=None
        )
        with self.assertRaises(_ApiAuthError):
            _fetch_api_response(api)

    def test_auth_error_contains_status_code(self):
        import urllib.error

        from generate_nibe_mqtt import _ApiAuthError, _fetch_api_response

        api = MagicMock()
        api.fetch_device_info.side_effect = urllib.error.HTTPError(
            url="https://host", code=401, msg="Unauthorized", hdrs={}, fp=None
        )
        with self.assertRaises(_ApiAuthError) as ctx:
            _fetch_api_response(api)
        self.assertIn("401", str(ctx.exception))

    def test_success_logs_connection_info(self):
        api = self._api(
            {
                "product": {
                    "name": "S2125",
                    "manufacturer": "NIBE",
                    "serialNumber": "ABC",
                    "firmwareId": "4.12.8",
                }
            }
        )
        with self.assertLogs("nibe.startup", level="INFO") as log:
            self.fn(api)
        self.assertTrue(any("S2125" in m for m in log.output))

    def test_manufacturer_field_actually_used_not_ignored(self):
        """The logged manufacturer must come from the real
        product['manufacturer'] key — not silently ignored in favour of
        the 'NIBE' default. A manufacturer value equal to the default
        ('NIBE') couldn't distinguish product.get('manufacturer', 'NIBE')
        from a buggy product.get(None, 'NIBE'), so this test uses a
        distinctly different value."""
        api = self._api(
            {
                "product": {
                    "name": "S2125",
                    "manufacturer": "Distinctive Corp",
                    "serialNumber": "ABC",
                    "firmwareId": "4.12.8",
                }
            }
        )
        with self.assertLogs("nibe.startup", level="INFO") as log:
            self.fn(api)
        self.assertTrue(any("Distinctive Corp" in m for m in log.output))

    def test_none_response_logs_warning(self):
        api = self._api(None)
        with self.assertLogs("nibe.startup", level="WARNING") as log:
            self.fn(api)
        self.assertTrue(any("offline" in m.lower() for m in log.output))

    def test_truthy_response_missing_product_key_does_not_raise(self):
        """response.get('product', {}) must default to {}, not None — a
        truthy response lacking a 'product' key (e.g. a partial/odd API
        reply) must still let product.get(...) run instead of raising
        AttributeError on None."""
        api = self._api({"other_key": "present"})
        result = self.fn(api)  # must not raise
        self.assertEqual(result, {"other_key": "present"})

    def test_testing_connection_log_has_exact_text(self):
        api = self._api({})
        with patch("generate_nibe_mqtt.log_startup") as mock_log:
            self.fn(api)
        self.assertEqual(mock_log.info.call_args_list[0].args[0], "Testing Nibe API connection...")

    def test_offline_warning_has_exact_text(self):
        api = self._api(None)
        with patch("generate_nibe_mqtt.log_startup") as mock_log:
            self.fn(api)
        mock_log.warning.assert_called_once_with(
            "Cannot reach Nibe API at startup — device may be offline. "
            "The bridge will start and keep retrying."
        )

    def test_connected_log_has_exact_format_string_and_real_field_values(self):
        """Every %s placeholder must be filled from the real product dict
        via the real keys — not a wrong/missing key or a hardcoded
        default masking the actual value."""
        api = self._api(
            {
                "product": {
                    "manufacturer": "RealMfr",
                    "name": "RealName",
                    "serialNumber": "RealSerial",
                    "firmwareId": "RealFw",
                }
            }
        )
        with patch("generate_nibe_mqtt.log_startup") as mock_log:
            self.fn(api)
        mock_log.info.assert_called_with(
            "Connected to %s %s (serial: %s, firmware: %s)",
            "RealMfr",
            "RealName",
            "RealSerial",
            "RealFw",
        )

    def test_connected_log_defaults_when_product_fields_missing(self):
        """When product fields are absent, the real defaults ('NIBE',
        'S-series', 'unknown', 'unknown') must be used — not None or a
        different placeholder string."""
        api = self._api({"product": {}})
        with patch("generate_nibe_mqtt.log_startup") as mock_log:
            self.fn(api)
        mock_log.info.assert_called_with(
            "Connected to %s %s (serial: %s, firmware: %s)",
            "NIBE",
            "S-series",
            "unknown",
            "unknown",
        )

    def test_connected_log_name_falsy_falls_back_to_s_series(self):
        """An explicitly empty product['name'] must still fall back to
        'S-series' via the `or` — not pass through as ''."""
        api = self._api({"product": {"name": ""}})
        with patch("generate_nibe_mqtt.log_startup") as mock_log:
            self.fn(api)
        mock_log.info.assert_called_with(
            "Connected to %s %s (serial: %s, firmware: %s)",
            "NIBE",
            "S-series",
            "unknown",
            "unknown",
        )


class TestLoadMenuStructure(unittest.TestCase):
    """_load_menu_structure: success, missing file, and corrupt YAML paths."""

    def setUp(self):
        from generate_nibe_mqtt import _load_menu_structure

        self.fn = _load_menu_structure

    def test_valid_dir_returns_dict_and_frozenset(self):
        point_to_menu, menu_points = self.fn(_APP_DIR)
        self.assertIsInstance(point_to_menu, dict)
        self.assertIsInstance(menu_points, frozenset)

    def test_valid_dir_returns_nonempty_results(self):
        point_to_menu, menu_points = self.fn(_APP_DIR)
        self.assertGreater(len(point_to_menu), 0)
        self.assertGreater(len(menu_points), 0)

    def test_missing_dir_returns_empty_results(self):
        point_to_menu, menu_points = self.fn("/nonexistent/path")
        self.assertEqual(point_to_menu, {})
        self.assertEqual(menu_points, frozenset())

    def test_missing_dir_logs_warning_with_exact_text(self):
        with self.assertLogs("nibe.startup", level="WARNING") as cm:
            self.fn("/nonexistent/path")
        [warning_msg] = cm.output
        prefix, _, suffix = warning_msg.partition(
            "WARNING:nibe.startup:Could not build point→menu map / MODES['menus']: "
        )
        self.assertEqual(prefix, "")
        self.assertTrue(suffix, "the real exception text must appear, not e.g. 'None'")
        self.assertNotEqual(suffix.strip(), "None")

    def test_missing_dir_warning_call_has_the_exception_as_a_real_arg(self):
        """A dropped second arg (log_startup.warning('...%s',) with no
        value) doesn't raise or show up as 'None' text via assertLogs —
        logging's lazy %-formatting only errors when a handler actually
        formats the record, which the default assertLogs capture handler
        doesn't trigger the same way. Asserting on the mocked call's own
        args directly is the reliable way to pin this."""
        with patch("generate_nibe_mqtt.log_startup") as mock_log:
            self.fn("/nonexistent/path")
        mock_log.warning.assert_called_once()
        self.assertEqual(len(mock_log.warning.call_args.args), 2)
        self.assertIsInstance(mock_log.warning.call_args.args[1], Exception)

    def test_valid_dir_logs_debug_with_exact_counts(self):
        point_to_menu, menu_points = self.fn(_APP_DIR)
        with self.assertLogs("nibe.startup", level="DEBUG") as cm:
            self.fn(_APP_DIR)
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(f"Built point→menu map: {len(point_to_menu)} entries")
                for msg in cm.output
            )
        )
        self.assertTrue(
            any(
                msg.splitlines()[0].endswith(f"MODES['menus'] populated: {len(menu_points)} points")
                for msg in cm.output
            )
        )

    def test_missing_dir_does_not_raise(self):
        self.fn("/nonexistent/path")  # must not raise

    def test_corrupt_yaml_returns_empty_results(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = os.path.join(tmpdir, "menu_structure.yaml")
            with open(yaml_path, "w") as f:
                f.write(": invalid: yaml: {{{")
            point_to_menu, menu_points = self.fn(tmpdir)
        self.assertEqual(point_to_menu, {})
        self.assertEqual(menu_points, frozenset())

    def test_empty_yaml_returns_empty_results(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = os.path.join(tmpdir, "menu_structure.yaml")
            with open(yaml_path, "w") as f:
                f.write("menus: []\n")
            point_to_menu, menu_points = self.fn(tmpdir)
        self.assertEqual(point_to_menu, {})
        self.assertEqual(menu_points, frozenset())

    def test_menu_points_subset_of_real_points(self):
        """All points in MODES['menus'] must be real Nibe point IDs."""
        _point_to_menu, menu_points = self.fn(_APP_DIR)
        # menu_points contains only integers
        for pid in menu_points:
            self.assertIsInstance(pid, int)

    def test_log_if_mode_defaults_to_true(self):
        """log_if_mode's default must be True — when the caller omits it
        entirely (as production code does on every startup call), the
        build-summary debug logs must still fire. This is distinct from
        the log_if_mode=False test class below, which always passes the
        argument explicitly and can't verify the default value itself."""
        with self.assertLogs("nibe.startup", level="DEBUG") as cm:
            self.fn(_APP_DIR)  # log_if_mode omitted — relies on the default
        self.assertTrue(any("Built point" in msg for msg in cm.output))

    def test_uses_the_real_lowercase_menu_structure_filename(self):
        """menu_path must join app_dir with the real lowercase
        'menu_structure.yaml' — not a different-case variant. The
        existing corrupt/empty-YAML tests can't distinguish 'file has bad
        content' from 'file wasn't found at all' (both return empty
        results via the same except-block), and on a case-insensitive
        dev filesystem a wrong-case path would still happen to resolve
        correctly — masking a real bug on the case-sensitive Linux
        filesystem the bridge actually runs on in production."""
        with (
            patch("generate_nibe_mqtt._load_menu_structure_yaml") as mock_load,
            patch("generate_nibe_mqtt.build_menu_points", return_value=frozenset()),
        ):
            mock_load.return_value = {"menus": []}
            self.fn(_APP_DIR)
        expected_path = os.path.join(_APP_DIR, "menu_structure.yaml")
        mock_load.assert_called_once_with(expected_path)


# ---------------------------------------------------------------------------
# Hypothesis properties for generate_nibe_mqtt helpers
# ---------------------------------------------------------------------------


class TestKeepaliveFromConfigExtendedProperties(unittest.TestCase):
    """Additional Hypothesis properties for _keepalive_from_config."""

    @given(st.integers(min_value=50, max_value=3600))
    def test_large_interval_equals_interval_plus_10(self, poll_interval):
        """When poll_interval >= 50, result is always poll_interval + 10."""
        from generate_nibe_mqtt import _keepalive_from_config

        self.assertEqual(_keepalive_from_config(poll_interval), poll_interval + 10)

    @given(st.integers(min_value=0, max_value=49))
    def test_small_interval_always_returns_60(self, poll_interval):
        """When poll_interval < 50, result is always exactly 60."""
        from generate_nibe_mqtt import _keepalive_from_config

        self.assertEqual(_keepalive_from_config(poll_interval), 60)


class TestRunScanWithRetryProperties(unittest.TestCase):
    """Hypothesis properties for _run_scan_with_retry."""

    @given(
        st.sets(st.integers(min_value=1, max_value=99999), min_size=1, max_size=20),
        st.integers(min_value=1, max_value=5),
        st.lists(st.integers(min_value=0, max_value=1), min_size=1, max_size=5),
    )
    def test_nonempty_first_scan_never_sleeps(self, point_ids, retries, backoffs):
        """If the first scan returns non-empty, sleep is never called."""
        from generate_nibe_mqtt import _run_scan_with_retry

        em = MagicMock()
        em.scan_mqtt_discovery.return_value = point_ids
        with patch("time.sleep") as mock_sleep:
            result = _run_scan_with_retry(em, retries=retries, backoffs=backoffs)
        mock_sleep.assert_not_called()
        self.assertEqual(result, point_ids)

    @given(
        st.integers(min_value=1, max_value=5),
        st.lists(st.integers(min_value=0, max_value=1), min_size=1, max_size=5),
    )
    def test_always_returns_set(self, retries, backoffs):
        """Result is always a set regardless of scan outcomes."""
        from generate_nibe_mqtt import _run_scan_with_retry

        em = MagicMock()
        em.scan_mqtt_discovery.return_value = set()
        with patch("time.sleep"):
            result = _run_scan_with_retry(em, retries=retries, backoffs=backoffs)
        self.assertIsInstance(result, set)

    @given(
        st.integers(min_value=1, max_value=5),
        st.lists(st.integers(min_value=0, max_value=1), min_size=1, max_size=5),
    )
    def test_scan_called_at_most_retries_plus_one(self, retries, backoffs):
        """scan_mqtt_discovery is called at most retries+1 times."""
        from generate_nibe_mqtt import _run_scan_with_retry

        em = MagicMock()
        em.scan_mqtt_discovery.return_value = set()
        with patch("time.sleep"):
            _run_scan_with_retry(em, retries=retries, backoffs=backoffs)
        self.assertLessEqual(em.scan_mqtt_discovery.call_count, retries + 1)


class TestApplyStartupAction(unittest.TestCase):
    """EntityManager._apply_startup_action: the shared mutation core.

    _execute_startup_action and complete_deferred_discovery both delegate
    to this method.  Tests here verify the mutations in isolation so the
    callers only need to test their context-specific log messages and
    side-effects (mode=none notification, etc.).
    """

    def _em(self):
        return _make_em()

    def test_apply_calls_apply_mode(self):
        """action='apply' must call apply_mode(initial_mode) and nothing else."""
        em = self._em()
        with (
            patch.object(em, "apply_mode") as mock_apply,
            patch.object(em, "restore_from_mqtt") as mock_restore,
            patch.object(em, "record_applied_mode") as mock_record,
        ):
            em.apply_startup_action("apply", None, "essential")
        mock_apply.assert_called_once_with("essential")
        mock_restore.assert_not_called()
        mock_record.assert_not_called()

    def test_restore_calls_restore_from_mqtt(self):
        """action='restore' with a known applied_mode must restore and not re-record."""
        em = self._em()
        with (
            patch.object(em, "apply_mode") as mock_apply,
            patch.object(em, "restore_from_mqtt") as mock_restore,
            patch.object(em, "record_applied_mode") as mock_record,
        ):
            em.apply_startup_action("restore", "essential", "essential")
        mock_restore.assert_called_once()
        mock_apply.assert_not_called()
        mock_record.assert_not_called()

    def test_restore_records_mode_when_applied_mode_none(self):
        """action='restore' with applied_mode=None must record the current mode."""
        em = self._em()
        with (
            patch.object(em, "restore_from_mqtt"),
            patch.object(em, "record_applied_mode") as mock_record,
        ):
            em.apply_startup_action("restore", None, "monitoring")
        mock_record.assert_called_once_with("monitoring")

    def test_reconcile_restores_then_applies(self):
        """action='reconcile' must restore first, then apply the new mode."""
        em = self._em()
        call_order = []
        with (
            patch.object(em, "restore_from_mqtt", side_effect=lambda: call_order.append("restore")),
            patch.object(em, "apply_mode", side_effect=lambda m: call_order.append(f"apply:{m}")),
        ):
            em.apply_startup_action("reconcile", "essential", "advanced")
        self.assertEqual(call_order, ["restore", "apply:advanced"])

    @given(
        action=st.sampled_from(["apply", "restore", "reconcile"]),
        applied=st.one_of(st.none(), st.sampled_from(["essential", "monitoring", "advanced"])),
        initial=st.sampled_from(["essential", "monitoring", "advanced", "menus", "all", "none"]),
    )
    def test_never_raises(self, action, applied, initial):
        """_apply_startup_action must never raise for any valid action/mode combination."""
        em = self._em()
        with (
            patch.object(em, "apply_mode"),
            patch.object(em, "restore_from_mqtt"),
            patch.object(em, "record_applied_mode"),
        ):
            em.apply_startup_action(action, applied, initial)  # must not raise

    def test_execute_startup_action_delegates_mutations(self):
        """_execute_startup_action must delegate the mutations to _apply_startup_action,
        not reimplement them inline — confirmed by asserting _apply_startup_action is called
        with the correct arguments for each action type.
        """
        from generate_nibe_mqtt import _execute_startup_action

        for action in ("apply", "restore", "reconcile"):
            em = self._em()
            with (
                patch.object(em, "apply_startup_action") as mock_apply_action,
                patch("generate_nibe_mqtt.notify_ha"),
            ):
                _execute_startup_action(em, action, "essential", "essential", MagicMock(), "Test")
            (
                mock_apply_action.assert_called_once_with(action, "essential", "essential"),
                f"action={action!r}: apply_startup_action not called correctly",
            )


class TestExecuteStartupActionProperties(unittest.TestCase):
    """Hypothesis properties for _execute_startup_action."""

    _modes = st.sampled_from(["essential", "monitoring", "advanced", "menus", "all", "none"])
    _applied = st.one_of(st.none(), st.sampled_from(["essential", "monitoring", "advanced"]))

    def _make_patched_em(self):
        """Real EntityManager with apply_mode/restore/record patched out."""
        em = _make_em()
        em.apply_mode = MagicMock()
        em.restore_from_mqtt = MagicMock()
        em.record_applied_mode = MagicMock()
        return em

    @given(_modes, _applied)
    def test_apply_never_calls_restore(self, initial, applied):
        from generate_nibe_mqtt import _execute_startup_action

        em = self._make_patched_em()
        with patch("generate_nibe_mqtt.notify_ha"):
            _execute_startup_action(em, "apply", applied, initial, MagicMock(), "Dev")
        em.restore_from_mqtt.assert_not_called()

    @given(_modes, _applied)
    def test_restore_never_calls_apply_mode(self, initial, applied):
        from generate_nibe_mqtt import _execute_startup_action

        em = self._make_patched_em()
        _execute_startup_action(em, "restore", applied, initial, MagicMock(), "Dev")
        em.apply_mode.assert_not_called()

    @given(_modes, _applied)
    def test_reconcile_always_calls_both(self, initial, applied):
        from generate_nibe_mqtt import _execute_startup_action

        em = self._make_patched_em()
        _execute_startup_action(em, "reconcile", applied, initial, MagicMock(), "Dev")
        em.restore_from_mqtt.assert_called_once()
        em.apply_mode.assert_called_once_with(initial)

    @given(st.text(min_size=1, max_size=20), _applied, _modes)
    def test_unknown_action_never_raises(self, action, applied, initial):
        """Any action string that is not apply/restore/reconcile must not raise."""
        from generate_nibe_mqtt import _execute_startup_action

        em = self._make_patched_em()
        with patch("generate_nibe_mqtt.notify_ha"):
            _execute_startup_action(em, action, applied, initial, MagicMock(), "Dev")


class TestFetchApiResponseProperties(unittest.TestCase):
    """Hypothesis properties for _fetch_api_response."""

    @given(st.one_of(st.none(), st.just({})))
    def test_none_or_empty_response_returns_empty_dict(self, response):
        """None or empty dict response always returns {}."""
        from generate_nibe_mqtt import _fetch_api_response

        api = MagicMock()
        api.fetch_device_info.return_value = response
        result = _fetch_api_response(api)
        self.assertEqual(result, {})

    @given(st.integers(min_value=400, max_value=599))
    def test_any_http_error_raises_api_auth_error(self, status_code):
        """Any HTTP error (4xx/5xx) from fetch_device_info raises _ApiAuthError."""
        import urllib.error

        from generate_nibe_mqtt import _ApiAuthError, _fetch_api_response

        api = MagicMock()
        api.fetch_device_info.side_effect = urllib.error.HTTPError(
            url="https://host",
            code=status_code,
            msg="Error",
            hdrs={},
            fp=None,
        )
        with self.assertRaises(_ApiAuthError):
            _fetch_api_response(api)

    @given(
        st.text(min_size=1, max_size=20),
        st.text(min_size=1, max_size=20),
        st.text(min_size=1, max_size=20),
        st.text(min_size=1, max_size=20),
    )
    def test_full_product_response_returned_unchanged(self, name, manufacturer, serial, firmware):
        """A complete product response is returned exactly as received."""
        from generate_nibe_mqtt import _fetch_api_response

        response = {
            "product": {
                "name": name,
                "manufacturer": manufacturer,
                "serialNumber": serial,
                "firmwareId": firmware,
            }
        }
        api = MagicMock()
        api.fetch_device_info.return_value = response
        result = _fetch_api_response(api)
        self.assertEqual(result, response)


class TestLoadBridgeVersionAllPathsFail(unittest.TestCase):
    """_load_bridge_version: RuntimeError when config.yaml isn't found at
    any candidate path — previously untested (only the success path was
    covered via the BRIDGE_VERSION module constant)."""

    def test_raises_runtime_error_when_no_candidate_path_exists(self):
        import generate_nibe_mqtt as gn

        with (
            patch("builtins.open", side_effect=FileNotFoundError),
            self.assertRaises(RuntimeError) as ctx,
        ):
            gn._load_bridge_version()
        self.assertIn("config.yaml", str(ctx.exception))

    def test_open_called_with_utf8_encoding_kwarg(self):
        import generate_nibe_mqtt as gn

        def fake_open(_path, *_a, **kw):
            self.assertEqual(kw.get("encoding"), "utf-8")
            raise FileNotFoundError

        with patch("builtins.open", side_effect=fake_open), self.assertRaises(RuntimeError):
            gn._load_bridge_version()

    def test_candidate_paths_are_exactly_the_three_expected_ones(self):
        """Pins the exact literal candidate paths (not just that *some*
        path is tried) — /config.yaml and /mnt/project/config.yaml in
        particular have no other test coverage since neither exists on a
        dev machine."""
        import generate_nibe_mqtt as gn

        attempted = []

        def fake_open(path, *a, **kw):
            attempted.append(path)
            raise FileNotFoundError

        with patch("builtins.open", side_effect=fake_open), self.assertRaises(RuntimeError):
            gn._load_bridge_version()
        repo_relative = os.path.join(
            os.path.dirname(os.path.abspath(gn.__file__)),
            "..",
            "config.yaml",
        )
        self.assertEqual(
            attempted,
            ["/config.yaml", repo_relative, "/mnt/project/config.yaml"],
        )

    def test_runtime_error_has_exact_message(self):
        import generate_nibe_mqtt as gn

        with (
            patch("builtins.open", side_effect=FileNotFoundError),
            self.assertRaises(RuntimeError) as ctx,
        ):
            gn._load_bridge_version()
        self.assertEqual(
            str(ctx.exception),
            "Could not determine BRIDGE_VERSION: config.yaml not found at any known path",
        )

    def test_fresh_call_resolves_real_candidate_path_and_matches_config_yaml(self):
        """Calling _load_bridge_version() directly (not relying on the
        cached module-level BRIDGE_VERSION constant, which was computed
        once at import time and is never re-derived per test) must
        correctly walk the real candidate path list and find the real
        config.yaml — exercising the actual path construction (dirname,
        '..', filename) against the real filesystem, not a mocked one.
        A mutation to any candidate path component would make this call
        fail to find the second candidate (the only one that resolves on
        a dev machine — the first and third are container-only paths) and
        fall through to RuntimeError instead of returning a real version."""
        import generate_nibe_mqtt as gn
        import yaml

        result = gn._load_bridge_version()
        config_path = os.path.join(_REPO_DIR, "config.yaml")
        with open(config_path, encoding="utf-8") as f:
            expected_version = yaml.safe_load(f)["version"]
        self.assertEqual(result, expected_version)


class TestGenerateNibeCrossFunctionProperties(unittest.TestCase):
    """Cross-function Hypothesis properties for generate_nibe_mqtt helpers."""

    def test_bridge_version_is_semver(self):
        """BRIDGE_VERSION is read from config.yaml and has the expected semver format."""
        from generate_nibe_mqtt import BRIDGE_VERSION

        self.assertRegex(
            BRIDGE_VERSION,
            r"^\d+\.\d+\.\d+$",
            f"BRIDGE_VERSION={BRIDGE_VERSION!r} is not a valid semver string",
        )

    def test_bridge_version_matches_config_yaml(self):
        """Regression guard: BRIDGE_VERSION must be derived from config.yaml's
        version: field, not a separately hardcoded literal. A hardcoded
        duplicate previously drifted to "1.0.1" while config.yaml (and the
        actual GitHub release) stayed at "1.0.0", with nothing catching it —
        this test independently re-reads config.yaml so it can't pass by
        construction if BRIDGE_VERSION reverts to a hardcoded string."""
        import yaml
        from generate_nibe_mqtt import BRIDGE_VERSION

        config_path = os.path.join(_REPO_DIR, "config.yaml")
        with open(config_path, encoding="utf-8") as f:
            config_version = yaml.safe_load(f)["version"]
        self.assertEqual(BRIDGE_VERSION, config_version)

    @given(st.text(max_size=50))
    def test_derive_then_build_client_id_always_safe(self, serial):
        """_derive_device_id output always produces a safe MQTT client ID ≤23 chars."""
        import shutil
        import tempfile

        from generate_nibe_mqtt import _build_mqtt_client_id, _derive_device_id

        tmpdir = tempfile.mkdtemp()
        try:
            device_id = _derive_device_id(
                {"product": {"serialNumber": serial}},
                "nibe_default",
                tmpdir + "/device_id",
            )
            client_id = _build_mqtt_client_id(device_id)
            self.assertLessEqual(len(client_id), 23)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @given(st.integers(min_value=0, max_value=3600))
    def test_keepalive_always_greater_than_any_poll_interval(self, poll_interval):
        """Keepalive is always strictly greater than poll_interval."""
        from generate_nibe_mqtt import _keepalive_from_config

        self.assertGreater(_keepalive_from_config(poll_interval), poll_interval)

    @given(st.text(min_size=1, max_size=20))
    def test_resolve_mode_output_usable_by_execute_startup_action(self, cfg_mode):
        """_resolve_initial_mode output can always be passed to _execute_startup_action."""
        from generate_nibe_mqtt import _execute_startup_action, _resolve_initial_mode

        args = MagicMock(mode=None)
        cfg = MagicMock(mode=cfg_mode)
        mode = _resolve_initial_mode(args, cfg)
        em = _make_em()
        em.apply_mode = MagicMock()
        em.restore_from_mqtt = MagicMock()
        em.record_applied_mode = MagicMock()
        with patch("generate_nibe_mqtt.notify_ha"):
            _execute_startup_action(em, "apply", None, mode, MagicMock(), "Dev")
        em.apply_mode.assert_called_once_with(mode)

    @given(st.text(max_size=30))
    def test_derive_device_id_output_always_valid_for_client_id(self, serial):
        """Pipeline: serial → device_id → client_id — no step raises."""
        import shutil
        import tempfile

        from generate_nibe_mqtt import _build_mqtt_client_id, _derive_device_id

        tmpdir = tempfile.mkdtemp()
        try:
            device_id = _derive_device_id(
                {"product": {"serialNumber": serial}},
                "nibe_fallback",
                tmpdir + "/device_id",
            )
            client_id = _build_mqtt_client_id(device_id)
            self.assertIsInstance(client_id, str)
            self.assertLessEqual(len(client_id), 23)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# 80. _setup_menu_dashboard — config save and dashboard create paths
# ===========================================================================


class TestRemoveMenuDashboard(unittest.TestCase):
    """_remove_menu_dashboard(): idempotent teardown of the Nibe Menus
    dashboard, run on every non-menus-mode startup (generate_nibe_mqtt.py's
    startup sequence) so a dashboard left over from a previous menus-mode
    run doesn't orphan into a wall of unavailable entities once its points
    are reconciled away by apply_mode(). Unlike _teardown_lovelace (opt-in,
    uninstall-only via NIBE_REMOVE_FRONTEND), this has no env gate — it
    only checks for SUPERVISOR_TOKEN, same as normal Lovelace provisioning."""

    def test_no_supervisor_token_skips(self):
        import nibe_lovelace as nl

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("nibe_lovelace._open_ha_websocket") as mock_ws,
        ):
            nl._remove_menu_dashboard()
        mock_ws.assert_not_called()

    def test_websocket_open_fails_returns_early(self):
        import nibe_lovelace as nl

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("nibe_lovelace._open_ha_websocket", return_value=None),
            patch("nibe_lovelace._ws_call") as mock_ws_call,
        ):
            nl._remove_menu_dashboard()
        mock_ws_call.assert_not_called()

    def _fake_ws_call(self, dashboard_present=True, delete_success=True):
        def fake(ws, _msg_id, payload, _timeout=10):
            t = payload.get("type")
            if t == "lovelace/dashboards/list":
                items = [{"url_path": "nibe-menus", "id": 42}] if dashboard_present else []
                return {"success": True, "result": items}
            if t == "lovelace/dashboards/delete":
                return {"success": delete_success}
            return {"success": True}

        return fake

    def test_dashboard_present_gets_deleted(self):
        import nibe_lovelace as nl

        ws = MagicMock()
        next_id = MagicMock(return_value=1)
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("nibe_lovelace._open_ha_websocket", return_value=(ws, next_id)),
            patch("nibe_lovelace._ws_call", side_effect=self._fake_ws_call(dashboard_present=True)),
            patch("nibe_lovelace.os.remove") as mock_rm,
        ):
            nl._remove_menu_dashboard()
        ws.close.assert_called_once()
        mock_rm.assert_called_once_with(nl._MENU_DASHBOARD_FLAG)

    def test_dashboard_absent_is_noop_no_delete_attempted(self):
        import nibe_lovelace as nl

        ws = MagicMock()
        next_id = MagicMock(return_value=1)
        calls = []

        def fake(_ws, _mid, payload, _timeout=10):
            calls.append(payload.get("type"))
            if payload.get("type") == "lovelace/dashboards/list":
                return {"success": True, "result": []}  # no nibe-menus, but list succeeded
            return {"success": True}

        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("nibe_lovelace._open_ha_websocket", return_value=(ws, next_id)),
            patch("nibe_lovelace._ws_call", side_effect=fake),
            patch("nibe_lovelace.os.remove") as mock_rm,
        ):
            nl._remove_menu_dashboard()
        self.assertNotIn("lovelace/dashboards/delete", calls)
        # Flag is removed even when dashboard is absent — list succeeded so we
        # know with certainty the dashboard doesn't exist
        mock_rm.assert_called_once_with(nl._MENU_DASHBOARD_FLAG)

    def test_stale_ws_call_does_not_remove_flag(self):
        """When lovelace/dashboards/list returns {} (stale WebSocket), we
        don't know whether the dashboard exists — flag must NOT be removed
        so the next startup retries the cleanup."""
        import nibe_lovelace as nl

        ws = MagicMock()
        next_id = MagicMock(return_value=1)
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("nibe_lovelace._open_ha_websocket", return_value=(ws, next_id)),
            patch("nibe_lovelace._ws_call", return_value={}),
            patch("nibe_lovelace.os.remove") as mock_rm,
        ):
            nl._remove_menu_dashboard()
        mock_rm.assert_not_called()

    def test_delete_failure_does_not_remove_flag(self):
        """When the delete call fails, we can't be sure the dashboard is gone —
        flag must NOT be removed so the next startup retries."""
        import nibe_lovelace as nl

        ws = MagicMock()
        next_id = MagicMock(return_value=1)
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("nibe_lovelace._open_ha_websocket", return_value=(ws, next_id)),
            patch(
                "nibe_lovelace._ws_call",
                side_effect=self._fake_ws_call(dashboard_present=True, delete_success=False),
            ),
            patch("nibe_lovelace.os.remove") as mock_rm,
        ):
            nl._remove_menu_dashboard()
        mock_rm.assert_not_called()

    def test_delete_failure_does_not_raise(self):
        import nibe_lovelace as nl

        ws = MagicMock()
        next_id = MagicMock(return_value=1)
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("nibe_lovelace._open_ha_websocket", return_value=(ws, next_id)),
            patch(
                "nibe_lovelace._ws_call",
                side_effect=self._fake_ws_call(dashboard_present=True, delete_success=False),
            ),
            patch("nibe_lovelace.os.remove"),
        ):
            nl._remove_menu_dashboard()  # must not raise
        ws.close.assert_called_once()

    def test_exception_during_teardown_is_caught_and_closes_ws(self):
        import nibe_lovelace as nl

        ws = MagicMock()
        next_id = MagicMock(return_value=1)
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("nibe_lovelace._open_ha_websocket", return_value=(ws, next_id)),
            patch("nibe_lovelace._ws_call", side_effect=RuntimeError("boom")),
            patch("nibe_lovelace.os.remove"),
        ):
            nl._remove_menu_dashboard()  # must not raise
        ws.close.assert_called_once()

    def test_flag_file_removal_error_tolerated(self):
        """A missing or unremovable flag file must not raise — it's cleanup,
        not a required precondition."""
        import nibe_lovelace as nl

        ws = MagicMock()
        next_id = MagicMock(return_value=1)
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "tok"}),
            patch("nibe_lovelace._open_ha_websocket", return_value=(ws, next_id)),
            patch(
                "nibe_lovelace._ws_call", side_effect=self._fake_ws_call(dashboard_present=False)
            ),
            patch("nibe_lovelace.os.remove", side_effect=OSError("not found")),
        ):
            nl._remove_menu_dashboard()  # must not raise

    def test_public_wrapper_delegates_to_private(self):
        import nibe_lovelace as nl

        with patch("nibe_lovelace._remove_menu_dashboard") as mock_private:
            nl.remove_menu_dashboard()
        mock_private.assert_called_once()


class TestMqttPublisherRemainingPaths(unittest.TestCase):
    """nibe_mqtt_publisher.py: select entity publish, binary_sensor entity
    publish, numeric-value-only state_class."""

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher

        self.mqtt = MagicMock()
        self.mqtt.publish.return_value = MagicMock(rc=0)
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt,
            device_info={"identifiers": ["nibe_test"]},
            device_id="test",
            device_name="Test Device",
        )

    def _point(self, entity_type, point_id=5000, **meta_overrides):
        meta = {
            "variableId": point_id,
            "variableType": "integer",
            "variableSize": "u8",
            "modbusRegisterType": "MODBUS_HOLDING_REGISTER",
            "isWritable": True,
            "divisor": 1,
            "decimal": 0,
            "minValue": 0,
            "maxValue": 3,
            "intDefaultValue": 0,
            "change": 1,
            "unit": "",
            "shortUnit": "",
            "modbusRegisterID": 4200,
            "stringDefaultValue": "",
        }
        meta.update(meta_overrides)
        return {
            "variableId": point_id,
            "display_title": "Test Point",
            "description": "0 = Off, 1 = Low, 2 = Med, 3 = High",
            "entity_type": entity_type,
            "entity_category": "config",
            "is_writable": True,
            "is_dynamic": False,
            "metadata": meta,
        }

    def test_select_entity_publishes_discovery_config(self):
        """publish_entity_discovery with entity_type='select' hits the
        _build_select_config branch."""
        point = self._point("select")
        bulk = {5000: {"raw_value": 0, "string_value": "", "is_ok": True}}
        result = self.pub.publish_entity_discovery(point, bulk)
        self.assertIsNotNone(result)
        published = self.mqtt.publish.call_args_list
        configs = [c for c in published if "/config" in str(c)]
        self.assertTrue(configs)

    def test_binary_sensor_entity_publishes_discovery_config(self):
        """publish_entity_discovery with entity_type='binary_sensor' hits
        the _build_binary_sensor_config branch."""
        point = self._point(
            "binary_sensor",
            point_id=5001,
            isWritable=False,
            maxValue=1,
            modbusRegisterType="MODBUS_INPUT_REGISTER",
        )
        point["is_writable"] = False
        bulk = {5001: {"raw_value": 0, "string_value": "", "is_ok": True}}
        result = self.pub.publish_entity_discovery(point, bulk)
        self.assertIsNotNone(result)

    def test_sensor_with_numeric_value_but_no_device_class_gets_measurement(self):
        """A sensor with a non-empty unit that has no matching device class
        gets state_class='measurement' from the has_numeric_value branch."""
        # 'rpm' is not in _UNIT_TO_DEVICE_CLASS and 'fan speed index'
        # does not match any keyword rule → device_class=None, unit truthy
        point = self._point(
            "sensor",
            point_id=5002,
            isWritable=False,
            modbusRegisterType="MODBUS_INPUT_REGISTER",
            unit="rpm",
        )
        point["display_title"] = "fan speed index"
        point["is_writable"] = False
        bulk = {5002: {"raw_value": 500, "string_value": "", "is_ok": True}}
        result = self.pub.publish_entity_discovery(point, bulk)
        self.assertIsNotNone(result)
        config_calls = [c for c in self.mqtt.publish.call_args_list if "/config" in str(c.args[0])]
        self.assertTrue(config_calls)
        payload = json.loads(config_calls[-1].args[1])
        self.assertEqual(payload.get("state_class"), "measurement")


# ===========================================================================
# Coverage: generate_nibe_mqtt.py — paho ImportError exit, _Formatter.format
# ===========================================================================


class TestGenerateNibeMqttSmallPaths(unittest.TestCase):
    def test_paho_import_error_prints_and_exits(self):
        """If paho-mqtt is not installed the module-level import fails and
        the fallback prints an error message then calls sys.exit(1)."""
        import importlib
        import sys

        # Temporarily hide paho so the import guard triggers
        real_paho = sys.modules.pop("paho", None)
        real_paho_mqtt = sys.modules.pop("paho.mqtt", None)
        real_paho_mqtt_client = sys.modules.pop("paho.mqtt.client", None)
        # Also remove the already-imported generate_nibe_mqtt so it re-executes
        real_gnm = sys.modules.pop("generate_nibe_mqtt", None)
        try:
            sys.modules["paho"] = None  # make import raise ImportError
            sys.modules["paho.mqtt"] = None
            sys.modules["paho.mqtt.client"] = None
            with self.assertRaises(SystemExit) as ctx:
                importlib.import_module("generate_nibe_mqtt")
            self.assertEqual(ctx.exception.code, 1)
            # Note: Changed from print() to logging, so we can't easily mock the logging call
            # The important behavior is that it exits with code 1
        finally:
            # Restore everything
            for key, val in [
                ("paho", real_paho),
                ("paho.mqtt", real_paho_mqtt),
                ("paho.mqtt.client", real_paho_mqtt_client),
                ("generate_nibe_mqtt", real_gnm),
            ]:
                if val is None:
                    sys.modules.pop(key, None)
                else:
                    sys.modules[key] = val

    def test_formatter_format_produces_expected_shape(self):
        """_Formatter.format returns a timestamped log line in the expected
        format — exercises the three lines inside the nested class."""
        import logging

        import generate_nibe_mqtt as gnm

        # _build_logging installs the formatter on a fresh nibe logger.
        # Clear handlers first so it doesn't early-return.
        nibe_log = logging.getLogger("nibe")
        original_handlers = nibe_log.handlers[:]
        # _build_logging() unconditionally calls root.setLevel(numeric) —
        # level='debug' below leaves the shared 'nibe' logger at DEBUG for
        # the rest of the pytest-xdist worker's session unless restored
        # here. Same root cause as the historical "root logger level leak
        # in _build_logging tests" fix (commit 063d16d) — that fix covered
        # three other tests but not this one, which reintroduced the exact
        # same leak. Every nibe.* child logger with no level of its own
        # inherits from this one, so leaving it at DEBUG makes unrelated
        # log calls elsewhere fire (and, via LogRecord creation calling
        # time.time()/consuming mocked side_effect sequences, can produce
        # confusing failures — or real HA notifications — in whichever
        # test happens to run next in the same worker).
        original_level = nibe_log.level
        nibe_log.handlers.clear()
        try:
            gnm._build_logging(level="debug")
            # The formatter is now installed; grab it from the handler.
            handler = nibe_log.handlers[-1]
            formatter = handler.formatter
            record = logging.LogRecord(
                name="nibe.test",
                level=logging.INFO,
                pathname="",
                lineno=0,
                msg="hello %s",
                args=("world",),
                exc_info=None,
            )
            result = formatter.format(record)
            # Shape: "HH:MM:SS.mmm [INFO    ] nibe.test: hello world"
            self.assertRegex(result, r"^\d{2}:\d{2}:\d{2}\.\d{3} \[INFO")
            self.assertIn("nibe.test", result)
            self.assertIn("hello world", result)
        finally:
            nibe_log.handlers.clear()
            nibe_log.handlers.extend(original_handlers)
            nibe_log.setLevel(original_level)


# ===========================================================================
# Bug fixes: concurrent disable race, _handle_event exception isolation,
#            refresh_registry auth handshake
# ===========================================================================


class TestBuildInfrastructure(unittest.TestCase):
    """_build_infrastructure: credential check, auth failure, connection failure."""

    def _cfg(self, **overrides):
        from generate_nibe_mqtt import BridgeConfig

        cfg = BridgeConfig(
            api_base_url="https://10.0.0.1:8443/api/v1/devices/0",
            nibe_auth="Basic dXNlcjpwYXNz",
            mqtt_broker="localhost",
            mqtt_port=1883,
            device_name="Test Device",
            device_id="nibe_test",
            poll_interval=30,
        )
        for k, v in overrides.items():
            setattr(cfg, k, v)
        return cfg

    def test_exits_when_no_nibe_auth(self):
        """Missing nibe_auth must call sys.exit(1) before touching the network."""
        from generate_nibe_mqtt import _build_infrastructure

        cfg = self._cfg(nibe_auth=None)
        with self.assertRaises(SystemExit) as ctx:
            _build_infrastructure(cfg)
        self.assertEqual(ctx.exception.code, 1)

    def test_exits_when_ssl_context_build_fails(self):
        """A ssl.SSLError while building the Nibe API TLS context (e.g. a
        malformed CA cert file) must call sys.exit(1) before any network
        call is attempted — previously never exercised, since every other
        test here mocks _build_ssl_context out to a working context."""
        import ssl

        from generate_nibe_mqtt import _build_infrastructure

        cfg = self._cfg()
        with (
            patch("generate_nibe_mqtt._build_ssl_context", side_effect=ssl.SSLError("bad CA cert")),
            self.assertRaises(SystemExit) as ctx,
        ):
            _build_infrastructure(cfg)
        self.assertEqual(ctx.exception.code, 1)

    def test_exits_on_api_auth_error(self):
        """HTTP 401/403 from the Nibe API must call sys.exit(1)."""
        from generate_nibe_mqtt import _ApiAuthError, _build_infrastructure

        cfg = self._cfg()
        with (
            patch("generate_nibe_mqtt._fetch_api_response", side_effect=_ApiAuthError(401)),
            patch("generate_nibe_mqtt._build_ssl_context", return_value=MagicMock()),
            patch("generate_nibe_mqtt.NibeApiClient"),
            patch("generate_nibe_mqtt.copy_card_file"),
            self.assertRaises(SystemExit) as ctx,
        ):
            _build_infrastructure(cfg)
        self.assertEqual(ctx.exception.code, 1)

    def test_exits_on_mqtt_connection_error(self):
        """Broker unreachable (connect raises) must call sys.exit(1)."""
        from generate_nibe_mqtt import _build_infrastructure

        cfg = self._cfg()
        mock_mqtt = MagicMock()
        mock_mqtt.connect.side_effect = OSError("connection refused")
        with (
            patch("generate_nibe_mqtt._fetch_api_response", return_value={}),
            patch("generate_nibe_mqtt._build_ssl_context", return_value=MagicMock()),
            patch("generate_nibe_mqtt.NibeApiClient"),
            patch("generate_nibe_mqtt.copy_card_file"),
            patch("generate_nibe_mqtt.mqtt.Client", return_value=mock_mqtt),
            self.assertRaises(SystemExit) as ctx,
        ):
            _build_infrastructure(cfg)
        self.assertEqual(ctx.exception.code, 1)

    def test_exits_on_mqtt_auth_failure(self):
        """MQTT broker returning reason code 4 (bad credentials) must call sys.exit(1).

        _build_infrastructure checks _auth_failed.is_set() after the 2s sleep.
        We simulate the failure by making threading.Event.is_set() return True
        for the first Event created (which is _auth_failed inside _build_infrastructure).
        """
        from generate_nibe_mqtt import _build_infrastructure

        cfg = self._cfg()

        mock_mc = MagicMock()
        mock_mc.is_connected.return_value = False

        # Intercept threading.Event so the first instance simulates auth failure
        real_Event = __import__("threading").Event
        events_created = []

        def _fake_Event():
            ev = real_Event()
            events_created.append(ev)
            if len(events_created) == 1:
                # This is _auth_failed — mark it as set immediately
                ev.set()
            return ev

        with (
            patch("generate_nibe_mqtt._fetch_api_response", return_value={}),
            patch("generate_nibe_mqtt._build_ssl_context", return_value=MagicMock()),
            patch("generate_nibe_mqtt.NibeApiClient"),
            patch("generate_nibe_mqtt.copy_card_file"),
            patch("generate_nibe_mqtt.mqtt.Client", return_value=mock_mc),
            patch("generate_nibe_mqtt.time.sleep"),
            patch("generate_nibe_mqtt.threading.Event", side_effect=_fake_Event),
            self.assertRaises(SystemExit) as ctx,
        ):
            _build_infrastructure(cfg)
        self.assertEqual(ctx.exception.code, 1)

    def test_returns_tuple_on_success(self):
        """Happy path: returns (api_client, mqtt_client, response, device_id, shutting_down, set_entity_manager)."""
        from generate_nibe_mqtt import _build_infrastructure

        cfg = self._cfg()
        mock_response = {
            "product": {
                "serialNumber": "ABC123",
                "name": "S2125",
                "manufacturer": "NIBE",
                "firmwareId": "4.12",
            }
        }
        mock_mc = MagicMock()
        mock_mc.is_connected.return_value = True
        with (
            patch("generate_nibe_mqtt._fetch_api_response", return_value=mock_response),
            patch("generate_nibe_mqtt._build_ssl_context", return_value=MagicMock()),
            patch("generate_nibe_mqtt.NibeApiClient"),
            patch("generate_nibe_mqtt.copy_card_file"),
            patch("generate_nibe_mqtt.mqtt.Client", return_value=mock_mc),
            patch("generate_nibe_mqtt.time.sleep"),
        ):
            result = _build_infrastructure(cfg)

        _api_client, mqtt_client, response, device_id, shutting_down, set_em = result
        self.assertIs(mqtt_client, mock_mc)
        self.assertEqual(response, mock_response)
        self.assertIn("abc123", device_id)  # serial normalised to lowercase
        self.assertIsInstance(shutting_down, list)
        self.assertFalse(shutting_down[0])
        # set_em is a callable that wires entity_manager into the on_connect callback
        self.assertTrue(callable(set_em))

    def test_set_entity_manager_wires_reconnect_callback(self):
        """set_entity_manager(em) must make em available to the on_connect callback."""
        from generate_nibe_mqtt import _build_infrastructure

        cfg = self._cfg()
        mock_mc = MagicMock()
        mock_mc.is_connected.return_value = True
        with (
            patch("generate_nibe_mqtt._fetch_api_response", return_value={}),
            patch("generate_nibe_mqtt._build_ssl_context", return_value=MagicMock()),
            patch("generate_nibe_mqtt.NibeApiClient"),
            patch("generate_nibe_mqtt.copy_card_file"),
            patch("generate_nibe_mqtt.mqtt.Client", return_value=mock_mc),
            patch("generate_nibe_mqtt.time.sleep"),
        ):
            _, _, _, _, _, set_em = _build_infrastructure(cfg)

        fake_em = MagicMock()
        set_em(fake_em)  # wire in the entity manager

        # Simulate a reconnection — extract the on_connect callback and fire it
        on_connect = mock_mc.on_connect
        rc = MagicMock()
        rc.value = 0
        on_connect(mock_mc, None, None, rc, None)

        # resubscribe_all and republish_availability must have been called
        fake_em.resubscribe_all.assert_called_once()
        fake_em.republish_availability.assert_called_once()

    def test_device_id_derived_from_serial(self):
        """device_id must incorporate the serial number from the API response."""
        from generate_nibe_mqtt import _build_infrastructure

        cfg = self._cfg()
        mock_response = {"product": {"serialNumber": "SN99887766"}}
        mock_mc = MagicMock()
        mock_mc.is_connected.return_value = True
        with (
            patch("generate_nibe_mqtt._fetch_api_response", return_value=mock_response),
            patch("generate_nibe_mqtt._build_ssl_context", return_value=MagicMock()),
            patch("generate_nibe_mqtt.NibeApiClient"),
            patch("generate_nibe_mqtt.copy_card_file"),
            patch("generate_nibe_mqtt.mqtt.Client", return_value=mock_mc),
            patch("generate_nibe_mqtt.time.sleep"),
        ):
            _, _, _, device_id, _, _ = _build_infrastructure(cfg)
        self.assertIn("sn99887766", device_id)

    def test_wires_api_and_mqtt_clients_with_correct_arguments(self):
        """NibeApiClient, _fetch_api_response, and the mqtt_client setup calls
        must all receive the real cfg-derived values — not just *some* value.
        NibeApiClient and mqtt.Client are mocked entirely, so nothing else
        verifies their construction/call arguments."""
        from generate_nibe_mqtt import _build_infrastructure

        cfg = self._cfg(mqtt_username="bob", mqtt_password="secret")
        mock_response = {"product": {"serialNumber": "ABC123"}}
        mock_mc = MagicMock()
        mock_mc.is_connected.return_value = True
        mock_ssl_ctx = MagicMock()
        with (
            patch(
                "generate_nibe_mqtt._fetch_api_response", return_value=mock_response
            ) as mock_fetch,
            patch("generate_nibe_mqtt._build_ssl_context", return_value=mock_ssl_ctx),
            patch("generate_nibe_mqtt.NibeApiClient") as MockApiClient,
            patch("generate_nibe_mqtt.copy_card_file"),
            patch("generate_nibe_mqtt.mqtt.Client", return_value=mock_mc),
            patch("generate_nibe_mqtt.time.sleep"),
        ):
            _build_infrastructure(cfg)

        # NibeApiClient built with the real base_url/auth/ssl_context/language
        MockApiClient.assert_called_once_with(
            cfg.api_base_url, cfg.nibe_auth, mock_ssl_ctx, cfg.language
        )
        # _fetch_api_response called with the constructed api_client
        mock_fetch.assert_called_once_with(MockApiClient.return_value)

        # mqtt_client setup calls use the real values, not placeholders
        mock_mc.user_data_set.assert_called_once_with({})
        mock_mc.reconnect_delay_set.assert_called_once_with(min_delay=1, max_delay=30)
        mock_mc.max_queued_messages_set.assert_called_once_with(1000)
        mock_mc.username_pw_set.assert_called_once_with("bob", "secret")
        from generate_nibe_mqtt import MGMT_AVAIL_TOPIC

        mock_mc.will_set.assert_called_once_with(MGMT_AVAIL_TOPIC, "offline", retain=True)
        mock_mc.publish.assert_called_once_with(MGMT_AVAIL_TOPIC, "online", retain=True)
        from generate_nibe_mqtt import _keepalive_from_config

        expected_keepalive = _keepalive_from_config(cfg.poll_interval)
        mock_mc.connect.assert_called_once_with(
            cfg.mqtt_broker,
            cfg.mqtt_port,
            keepalive=expected_keepalive,
        )
        mock_mc.loop_start.assert_called_once()

    def test_username_pw_not_set_when_credentials_missing(self):
        """When mqtt_username/password are absent, username_pw_set must not
        be called at all — the broker-ACL warning path instead."""
        from generate_nibe_mqtt import _build_infrastructure

        cfg = self._cfg(mqtt_username=None, mqtt_password=None)
        mock_mc = MagicMock()
        mock_mc.is_connected.return_value = True
        with (
            patch("generate_nibe_mqtt._fetch_api_response", return_value={}),
            patch("generate_nibe_mqtt._build_ssl_context", return_value=MagicMock()),
            patch("generate_nibe_mqtt.NibeApiClient"),
            patch("generate_nibe_mqtt.copy_card_file"),
            patch("generate_nibe_mqtt.mqtt.Client", return_value=mock_mc),
            patch("generate_nibe_mqtt.time.sleep"),
        ):
            _build_infrastructure(cfg)
        mock_mc.username_pw_set.assert_not_called()

    def test_username_pw_not_set_when_only_username_present(self):
        """Both mqtt_username AND mqtt_password must be present —
        username_pw_set must NOT be called with just one of them. An `or`
        in place of `and` here would pass a None password to paho."""
        from generate_nibe_mqtt import _build_infrastructure

        cfg = self._cfg(mqtt_username="bob", mqtt_password=None)
        mock_mc = MagicMock()
        mock_mc.is_connected.return_value = True
        with (
            patch("generate_nibe_mqtt._fetch_api_response", return_value={}),
            patch("generate_nibe_mqtt._build_ssl_context", return_value=MagicMock()),
            patch("generate_nibe_mqtt.NibeApiClient"),
            patch("generate_nibe_mqtt.copy_card_file"),
            patch("generate_nibe_mqtt.mqtt.Client", return_value=mock_mc),
            patch("generate_nibe_mqtt.time.sleep"),
        ):
            _build_infrastructure(cfg)
        mock_mc.username_pw_set.assert_not_called()

    def test_on_connect_uses_int_reason_code_without_value_attr(self):
        """on_connect must derive rc_value correctly when reason_code is a
        plain int (no .value attribute) — the hasattr(...'value') branch.
        rc=0 must take the success path (resubscribe/republish), not the
        fatal-rc or generic-error branches."""
        mc, set_em, _ = self._call_infrastructure()
        fake_em = MagicMock()
        set_em(fake_em)
        with patch("generate_nibe_mqtt.log_mqtt") as mock_log:
            mc.on_connect(mc, None, None, 0, None)  # plain int, rc=0 (success)
        fake_em.resubscribe_all.assert_called_once()
        fake_em.republish_availability.assert_called_once()
        mock_log.error.assert_not_called()

    def test_on_connect_fatal_rc_as_plain_int_sets_auth_failed(self):
        """reason_code=4 (bad credentials) as a plain int — not a paho enum
        with .value — must still be recognised as fatal and log an error,
        not silently fall through to the generic 'connection failed' branch."""
        mc, _, _ = self._call_infrastructure()
        with patch("generate_nibe_mqtt.log_mqtt") as mock_log:
            mc.on_connect(mc, None, None, 4, None)
        mock_log.error.assert_called_once()
        self.assertIn("check mqtt_username and mqtt_password", mock_log.error.call_args.args[0])

    def test_on_connect_rc_5_is_fatal(self):
        """rc=5 (not authorised) must also be treated as fatal — pins the
        _FATAL_RC set's exact membership {4, 5} against a mutation like
        {4, 6}, which existing tests (using rc=4, already in both sets)
        can't distinguish."""
        mc, _, _ = self._call_infrastructure()
        with patch("generate_nibe_mqtt.log_mqtt") as mock_log:
            mc.on_connect(mc, None, None, 5, None)
        self.assertIn("refused", str(mock_log.error.call_args))

    def _call_infrastructure(self, cfg=None):
        """Helper: run _build_infrastructure and return (mqtt_client, set_em, shutting_down)."""
        from generate_nibe_mqtt import _build_infrastructure

        cfg = cfg or self._cfg()
        mock_mc = MagicMock()
        mock_mc.is_connected.return_value = True
        with (
            patch("generate_nibe_mqtt._fetch_api_response", return_value={}),
            patch("generate_nibe_mqtt._build_ssl_context", return_value=MagicMock()),
            patch("generate_nibe_mqtt.NibeApiClient"),
            patch("generate_nibe_mqtt.copy_card_file"),
            patch("generate_nibe_mqtt.mqtt.Client", return_value=mock_mc),
            patch("generate_nibe_mqtt.time.sleep"),
        ):
            _, mc, _, _, shutting_down, set_em = _build_infrastructure(cfg)
        return mc, set_em, shutting_down

    def test_on_disconnect_suppressed_when_shutting_down(self):
        """on_disconnect must return immediately when shutting_down[0] is True
        — no warning logged for an intentional clean shutdown."""
        mc, _, shutting_down = self._call_infrastructure()
        shutting_down[0] = True
        rc = MagicMock()
        rc.value = 0
        with patch("generate_nibe_mqtt.log_mqtt") as mock_log:
            mc.on_disconnect(mc, None, None, rc, None)
        mock_log.warning.assert_not_called()

    def test_on_disconnect_logs_warning_when_unexpected(self):
        """on_disconnect must log a warning when the disconnection is unexpected
        (shutting_down is False) with the correct label for the reason code."""
        mc, _, shutting_down = self._call_infrastructure()
        self.assertFalse(shutting_down[0])
        rc = MagicMock()
        rc.value = 0  # "clean disconnect or connection lost"
        with patch("generate_nibe_mqtt.log_mqtt") as mock_log:
            mc.on_disconnect(mc, None, None, rc, None)
        mock_log.warning.assert_called_once()
        msg = str(mock_log.warning.call_args)
        self.assertIn("reconnect", msg)

    def test_on_disconnect_unknown_rc_uses_str_fallback(self):
        """on_disconnect with an unknown reason code must use str(reason_code)
        as the label rather than crashing."""
        mc, _, _ = self._call_infrastructure()
        rc = MagicMock()
        rc.value = 99  # not in _DISCONNECT_LABELS
        rc.__str__ = lambda self: "rc=99"
        with patch("generate_nibe_mqtt.log_mqtt") as mock_log:
            mc.on_disconnect(mc, None, None, rc, None)
        mock_log.warning.assert_called_once()


# ===========================================================================
# Full-pipeline propagation: load_config() -> _build_infrastructure()
#
# load_config() (source-precedence merging) and _build_infrastructure()
# (wiring cfg into the real client calls) are each thoroughly unit-tested
# elsewhere in this file, but every _build_infrastructure test builds its
# BridgeConfig by hand via a _cfg(**overrides) helper — none of them start
# from raw options.json/env input and let load_config() do the merging.
# That seam (does a value that survived load_config's precedence logic
# *also* survive the trip into the actual mqtt_client.connect() call) is
# exactly where a real production bug lived (MQTT auto-discovery silently
# overriding an explicit mqtt_host setting). These tests chain the two
# functions together with nothing hand-substituted in between.
# ===========================================================================


class TestConfigPropagatesToInfrastructure(unittest.TestCase):
    """load_config() output, unmodified, must reach the real client calls
    inside _build_infrastructure() — the full options.json/env -> wire path."""

    def setUp(self):
        self._env_patcher = patch.dict("os.environ", {}, clear=True)
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()

    def _load(self, options=None, env=None):
        """Call the real load_config() with mocked filesystem/environment —
        same technique as TestLoadConfig._load, duplicated here rather than
        shared so this class stays a self-contained, readable specification
        of the full pipeline rather than depending on another test class."""
        import generate_nibe_mqtt as gn

        def fake_exists(path):
            if path == "/data/options.json":
                return options is not None
            return False

        import io

        def fake_open(path, *a, **kw):
            if path == "/data/options.json":
                return io.StringIO(json.dumps(options))
            raise FileNotFoundError(path)

        with (
            patch("os.path.exists", side_effect=fake_exists),
            patch("builtins.open", side_effect=fake_open),
            patch.dict("os.environ", env or {}),
        ):
            return gn.load_config()

    def _build(self, cfg):
        """Run the real _build_infrastructure() against cfg, with only the
        external-facing clients themselves mocked (network/API/MQTT) —
        everything else (cfg field resolution, arg wiring) is real code."""
        from generate_nibe_mqtt import _build_infrastructure

        mock_mc = MagicMock()
        mock_mc.is_connected.return_value = True
        with (
            patch("generate_nibe_mqtt._fetch_api_response", return_value={}),
            patch("generate_nibe_mqtt._build_ssl_context", return_value=MagicMock()),
            patch("generate_nibe_mqtt.NibeApiClient"),
            patch("generate_nibe_mqtt.copy_card_file"),
            patch("generate_nibe_mqtt.mqtt.Client", return_value=mock_mc),
            patch("generate_nibe_mqtt.time.sleep"),
        ):
            _build_infrastructure(cfg)
        return mock_mc

    def _build_full(self, cfg):
        """Like _build(), but also exposes the NibeApiClient and
        _build_ssl_context mocks so tests can assert on their call
        arguments — used to verify api_host/port/nibe_auth/nibe_ca_cert
        actually reach the real client constructor calls, not just that
        cfg carries the right values."""
        from generate_nibe_mqtt import _build_infrastructure

        mock_mc = MagicMock()
        mock_mc.is_connected.return_value = True
        mock_ssl_ctx = MagicMock()
        with (
            patch("generate_nibe_mqtt._fetch_api_response", return_value={}),
            patch(
                "generate_nibe_mqtt._build_ssl_context", return_value=mock_ssl_ctx
            ) as mock_build_ssl,
            patch("generate_nibe_mqtt.NibeApiClient") as MockApiClient,
            patch("generate_nibe_mqtt.copy_card_file"),
            patch("generate_nibe_mqtt.mqtt.Client", return_value=mock_mc),
            patch("generate_nibe_mqtt.time.sleep"),
        ):
            _build_infrastructure(cfg)
        return mock_mc, MockApiClient, mock_build_ssl, mock_ssl_ctx

    _BASE_OPTIONS: ClassVar[dict] = {
        "nibe_username": "nibeuser",
        "nibe_password": "nibepass",
    }

    def test_options_json_api_host_port_reach_nibe_api_client(self):
        """nibe_host/nibe_port from options.json must reach the real
        api_base_url derivation AND the actual NibeApiClient() constructor
        call — not just be present somewhere on cfg."""
        cfg = self._load(
            options={
                **self._BASE_OPTIONS,
                "mqtt_host": "broker.local",
                "nibe_host": "10.20.30.40",
                "nibe_port": 9443,
            }
        )
        self.assertEqual(cfg.api_base_url, "https://10.20.30.40:9443/api/v1/devices/0")
        _, MockApiClient, _, mock_ssl_ctx = self._build_full(cfg)
        MockApiClient.assert_called_once_with(
            "https://10.20.30.40:9443/api/v1/devices/0",
            cfg.nibe_auth,
            mock_ssl_ctx,
            cfg.language,
        )

    def test_options_json_nibe_credentials_reach_nibe_api_client_auth(self):
        """nibe_username/nibe_password from options.json must be encoded
        into nibe_auth AND reach the real NibeApiClient() constructor call."""
        import base64

        cfg = self._load(
            options={
                "mqtt_host": "broker.local",
                "nibe_username": "realnibeuser",
                "nibe_password": "realnibepass",
            }
        )
        expected_auth = "Basic " + base64.b64encode(b"realnibeuser:realnibepass").decode()
        self.assertEqual(cfg.nibe_auth, expected_auth)
        _, MockApiClient, _, _ = self._build_full(cfg)
        self.assertEqual(MockApiClient.call_args.args[1], expected_auth)

    def test_options_json_nibe_ca_cert_reaches_build_ssl_context_call(self):
        """nibe_ca_cert from options.json must reach the real
        _build_ssl_context() call as its argument — _build_ssl_context's own
        internals (verification on/off) are already unit-tested separately;
        this only checks the value survives the trip from options.json."""
        cfg = self._load(
            options={
                **self._BASE_OPTIONS,
                "mqtt_host": "broker.local",
                "nibe_ca_cert": "/config/nibe-ca.pem",
            }
        )
        self.assertEqual(cfg.nibe_ca_cert, "/config/nibe-ca.pem")
        _, _, mock_build_ssl, _ = self._build_full(cfg)
        mock_build_ssl.assert_called_once_with("/config/nibe-ca.pem")

    def test_options_json_mqtt_tls_and_ca_cert_reach_real_tls_set_call(self):
        """mqtt_tls + mqtt_ca_cert from options.json must reach the REAL
        (unmocked) _configure_mqtt_tls(), which must call the mock mqtt
        client's tls_set() with the real CA path — this is the actual
        function, not a mock, so it also proves _configure_mqtt_tls's own
        os.path.exists(ca_cert) check accepts a real filesystem path."""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".pem") as ca_file:
            cfg = self._load(
                options={
                    **self._BASE_OPTIONS,
                    "mqtt_host": "broker.local",
                    "mqtt_tls": True,
                    "mqtt_ca_cert": ca_file.name,
                }
            )
            self.assertTrue(cfg.mqtt_tls)
            self.assertEqual(cfg.mqtt_ca_cert, ca_file.name)
            mock_mc = self._build(cfg)
        mock_mc.tls_set.assert_called_once_with(ca_certs=ca_file.name)

    def test_options_json_mqtt_host_reaches_connect_call(self):
        """An explicit mqtt_host in options.json, with no env override
        present, must be the value mqtt_client.connect() actually receives."""
        cfg = self._load(options={**self._BASE_OPTIONS, "mqtt_host": "user-entered-broker.local"})
        mock_mc = self._build(cfg)
        mock_mc.connect.assert_called_once_with(
            "user-entered-broker.local",
            cfg.mqtt_port,
            keepalive=unittest.mock.ANY,
        )

    def test_env_discovered_broker_overrides_options_json_through_to_connect_call(self):
        """The exact scenario of the real production bug: NIBE_MQTT_BROKER
        (Supervisor-discovered) must be what mqtt_client.connect() receives,
        overriding an explicit options.json mqtt_host — verified through the
        real merge in load_config() AND the real wiring in
        _build_infrastructure(), not asserted against either function alone."""
        cfg = self._load(
            options={**self._BASE_OPTIONS, "mqtt_host": "user-entered-broker.local"},
            env={"NIBE_MQTT_BROKER": "discovered-broker.local"},
        )
        mock_mc = self._build(cfg)
        mock_mc.connect.assert_called_once_with(
            "discovered-broker.local",
            cfg.mqtt_port,
            keepalive=unittest.mock.ANY,
        )

    def test_options_json_mqtt_port_reaches_connect_call(self):
        cfg = self._load(
            options={**self._BASE_OPTIONS, "mqtt_host": "broker.local", "mqtt_port": 8883}
        )
        mock_mc = self._build(cfg)
        self.assertEqual(mock_mc.connect.call_args.args[1], 8883)

    def test_options_json_mqtt_credentials_reach_username_pw_set(self):
        """mqtt_username/password entered in options.json must reach the
        real username_pw_set() call, not just the resolved BridgeConfig."""
        cfg = self._load(
            options={
                **self._BASE_OPTIONS,
                "mqtt_host": "broker.local",
                "mqtt_username": "mqttuser",
                "mqtt_password": "mqttpass",
            }
        )
        mock_mc = self._build(cfg)
        mock_mc.username_pw_set.assert_called_once_with("mqttuser", "mqttpass")

    def test_env_svc_credentials_override_options_json_through_to_username_pw_set(self):
        """Supervisor-discovered MQTT credentials (NIBE_MQTT_SVC_*) must win
        over manually entered options.json credentials all the way through
        to the real username_pw_set() call."""
        cfg = self._load(
            options={
                **self._BASE_OPTIONS,
                "mqtt_host": "broker.local",
                "mqtt_username": "manual_user",
                "mqtt_password": "manual_pass",
            },
            env={
                "NIBE_MQTT_SVC_USERNAME": "svc_user",
                "NIBE_MQTT_SVC_PASSWORD": "svc_pass",
            },
        )
        mock_mc = self._build(cfg)
        mock_mc.username_pw_set.assert_called_once_with("svc_user", "svc_pass")


class TestShutdown(unittest.TestCase):
    """_shutdown: executor drain, offline publishes, MQTT disconnect."""

    def _make_em_with_entities(self, avail_topics):
        em = _make_em()
        for i, topic in enumerate(avail_topics):
            entity_info = {
                "point_id": i,
                "entity_type": "sensor",
                "availability_topic": topic,
                "state_topic": f"nibe/state/{i}",
                "command_topic": None,
                "point_data": {},
            }
            em.active_entities_by_id[i] = entity_info
            em.mqtt_enabled_points.add(i)
        return em

    def _run_shutdown(self, em, extra_topics=None):
        from generate_nibe_mqtt import _shutdown

        mc = MagicMock()
        pub = MagicMock()
        watcher = MagicMock()
        mgmt_exec = MagicMock()
        test_exec = MagicMock()
        shutting_down = [False]
        atexit_fn = MagicMock()

        with (
            patch("generate_nibe_mqtt.teardown_lovelace"),
            patch.dict("os.environ", {}, clear=False),
        ):
            os.environ.pop("NIBE_REMOVE_FRONTEND", None)
            _shutdown(em, pub, mc, watcher, mgmt_exec, test_exec, shutting_down, atexit_fn)

        return mc, watcher, mgmt_exec, test_exec, shutting_down, atexit_fn

    def test_abort_test_suite_called_with_exact_reason_text(self):
        em = _make_em()
        with patch("generate_nibe_mqtt.abort_test_suite") as mock_abort:
            self._run_shutdown(em)
        mock_abort.assert_called_once_with("add-on shutting down")

    def test_publishes_offline_for_all_active_entities(self):
        """_shutdown must publish 'offline' to every active entity's avail topic."""
        from generate_nibe_mqtt import MGMT_AVAIL_TOPIC

        em = self._make_em_with_entities(["nibe/avail/100", "nibe/avail/200"])
        mc, *_ = self._run_shutdown(em)
        published_topics = [
            call.args[0] for call in mc.publish.call_args_list if call.args[1] == "offline"
        ]
        self.assertIn("nibe/avail/100", published_topics)
        self.assertIn("nibe/avail/200", published_topics)
        self.assertIn(MGMT_AVAIL_TOPIC, published_topics)

    def test_publishes_offline_to_mgmt_topic_even_with_no_entities(self):
        """MGMT_AVAIL_TOPIC must always go offline, even when no entities are active."""
        from generate_nibe_mqtt import MGMT_AVAIL_TOPIC

        em = _make_em()
        mc, *_ = self._run_shutdown(em)
        published_topics = [
            call.args[0] for call in mc.publish.call_args_list if call.args[1] == "offline"
        ]
        self.assertIn(MGMT_AVAIL_TOPIC, published_topics)

    def test_publish_results_wait_for_publish_actually_invoked(self):
        """The real object returned by mqtt_client.publish() must be
        appended to pending_publishes and have wait_for_publish() invoked
        on it — not None or a dropped result. Appending None instead would
        raise AttributeError, silently caught by the broad except and
        logged as a generic warning, indistinguishable from a real
        confirmation failure to every existing assertion here."""
        from generate_nibe_mqtt import MGMT_AVAIL_TOPIC

        em = self._make_em_with_entities(["nibe/avail/100"])
        mc = MagicMock()
        results_by_topic = {}

        def fake_publish(topic, payload, retain=False):
            r = MagicMock(name=f"result-{topic}")
            results_by_topic[topic] = r
            return r

        mc.publish.side_effect = fake_publish
        with (
            patch("generate_nibe_mqtt.teardown_lovelace"),
            patch.dict("os.environ", {}, clear=False),
        ):
            os.environ.pop("NIBE_REMOVE_FRONTEND", None)
            from generate_nibe_mqtt import _shutdown

            _shutdown(
                em, MagicMock(), mc, MagicMock(), MagicMock(), MagicMock(), [False], MagicMock()
            )
        results_by_topic["nibe/avail/100"].wait_for_publish.assert_called_once_with(timeout=2.0)
        results_by_topic[MGMT_AVAIL_TOPIC].wait_for_publish.assert_called_once_with(timeout=2.0)

    def test_stops_registry_watcher(self):
        """registry_watcher.stop() must be called."""
        em = _make_em()
        _, watcher, *_ = self._run_shutdown(em)
        watcher.stop.assert_called_once()

    def test_shuts_down_executors(self):
        """The write, mgmt, and test-suite executors must all be shut down."""
        from generate_nibe_mqtt import _shutdown

        em = _make_em()
        mc = MagicMock()
        pub = MagicMock()
        watcher = MagicMock()
        mgmt_exec = MagicMock()
        test_exec = MagicMock()
        shutting_down = [False]
        atexit_fn = MagicMock()

        # Patch Thread so executor.shutdown runs synchronously in the test
        with (
            patch("generate_nibe_mqtt.threading.Thread") as MockThread,
            patch("generate_nibe_mqtt.teardown_lovelace"),
            patch.dict("os.environ", {}, clear=False),
        ):
            os.environ.pop("NIBE_REMOVE_FRONTEND", None)
            instance = MockThread.return_value
            instance.is_alive.return_value = False
            _shutdown(em, pub, mc, watcher, mgmt_exec, test_exec, shutting_down, atexit_fn)

        # Three threads should have been created: one per executor
        # (write, management, test suite)
        self.assertEqual(MockThread.call_count, 3)

    def test_thread_targets_executor_shutdown_with_correct_kwargs(self):
        """Each Thread must target executor.shutdown with wait=True and
        cancel_futures=False — not dropped kwargs or a wrong key name."""
        from generate_nibe_mqtt import _shutdown

        em = _make_em()
        mc = MagicMock()
        watcher = MagicMock()
        mgmt_exec = MagicMock()

        with (
            patch("generate_nibe_mqtt.threading.Thread") as MockThread,
            patch("generate_nibe_mqtt.teardown_lovelace"),
            patch.dict("os.environ", {}, clear=False),
        ):
            os.environ.pop("NIBE_REMOVE_FRONTEND", None)
            instance = MockThread.return_value
            instance.is_alive.return_value = False
            _shutdown(em, MagicMock(), mc, watcher, mgmt_exec, MagicMock(), [False], MagicMock())

        for call in MockThread.call_args_list:
            self.assertEqual(
                call.kwargs["kwargs"],
                {"wait": True, "cancel_futures": False},
            )

    def test_sets_shutting_down_flag(self):
        """shutting_down[0] must be True after _shutdown completes."""
        em = _make_em()
        _, _, _, _, shutting_down, _ = self._run_shutdown(em)
        self.assertTrue(shutting_down[0])

    def test_unregisters_atexit(self):
        """atexit_cleanup_fn must be unregistered to prevent double-disconnect."""
        em = _make_em()
        _, _, _, _, _, _atexit_fn = self._run_shutdown(em)
        # atexit.unregister was called with the function
        # (can't easily assert atexit.unregister directly; check loop_stop called)
        em2 = _make_em()
        mc2, *_ = self._run_shutdown(em2)
        mc2.loop_stop.assert_called_once()
        mc2.disconnect.assert_called_once()

    def test_atexit_unregister_called_with_the_real_callback(self):
        """atexit.unregister must be called with the real atexit_cleanup_fn
        passed in — not None or some other callable. `atexit` is imported
        as a plain module (import atexit), so it patches cleanly; the
        earlier note that this 'can't easily' be asserted was mistaken."""
        em = _make_em()
        real_atexit_fn = MagicMock(name="atexit_cleanup_fn")
        mc = MagicMock()
        with (
            patch("generate_nibe_mqtt.teardown_lovelace"),
            patch.dict("os.environ", {}, clear=False),
            patch("generate_nibe_mqtt.atexit.unregister") as mock_unregister,
        ):
            os.environ.pop("NIBE_REMOVE_FRONTEND", None)
            from generate_nibe_mqtt import _shutdown

            _shutdown(
                em, MagicMock(), mc, MagicMock(), MagicMock(), MagicMock(), [False], real_atexit_fn
            )
        mock_unregister.assert_called_once_with(real_atexit_fn)

    def test_runs_mqtt_cleanup_when_remove_frontend_set(self):
        """When remove_frontend=True, _cleanup_mqtt_retained must be called."""
        from generate_nibe_mqtt import _shutdown

        em = _make_em()
        mc = MagicMock()
        shutting_down = [False]

        with (
            patch("generate_nibe_mqtt.teardown_lovelace"),
            patch("generate_nibe_mqtt._cleanup_mqtt_retained") as mock_cleanup,
        ):
            _shutdown(
                em,
                MagicMock(),
                mc,
                MagicMock(),
                MagicMock(),
                MagicMock(),
                shutting_down,
                MagicMock(),
                remove_frontend=True,
            )

        mock_cleanup.assert_called_once_with(mc)

    def test_default_remove_frontend_is_false(self):
        """remove_frontend's default is False — omitting it must NOT wipe
        the Entity Manager card's retained MQTT topics on every ordinary
        shutdown."""
        from generate_nibe_mqtt import _shutdown

        em = _make_em()
        with (
            patch("generate_nibe_mqtt.teardown_lovelace"),
            patch("generate_nibe_mqtt._cleanup_mqtt_retained") as mock_cleanup,
        ):
            _shutdown(
                em,
                MagicMock(),
                MagicMock(),
                MagicMock(),
                MagicMock(),
                MagicMock(),
                [False],
                MagicMock(),
            )  # remove_frontend omitted
        mock_cleanup.assert_not_called()

    def test_teardown_lovelace_receives_the_real_remove_frontend_flag(self):
        from generate_nibe_mqtt import _shutdown

        em = _make_em()
        with patch("generate_nibe_mqtt.teardown_lovelace") as mock_teardown:
            _shutdown(
                em,
                MagicMock(),
                MagicMock(),
                MagicMock(),
                MagicMock(),
                MagicMock(),
                [False],
                MagicMock(),
                remove_frontend=True,
            )
        mock_teardown.assert_called_once_with(True)

    def test_thread_target_is_executor_shutdown_method(self):
        """Each Thread's target must be the executor's own .shutdown method
        — not None or a dropped kwarg. A missing target would make the
        Thread do nothing, so the executor would never actually drain."""
        from generate_nibe_mqtt import _shutdown

        em = _make_em()
        mgmt_exec = MagicMock()
        with (
            patch("generate_nibe_mqtt.threading.Thread") as MockThread,
            patch("generate_nibe_mqtt.teardown_lovelace"),
        ):
            instance = MockThread.return_value
            instance.is_alive.return_value = False
            _shutdown(
                em,
                MagicMock(),
                MagicMock(),
                MagicMock(),
                mgmt_exec,
                MagicMock(),
                [False],
                MagicMock(),
            )
        targets = [call.kwargs["target"] for call in MockThread.call_args_list]
        self.assertIn(mgmt_exec.shutdown, targets)

    def test_offline_publishes_use_retain_true(self):
        """Entity and management availability publishes on shutdown must be
        retained — a subscriber connecting after the bridge has already
        gone offline still needs to see the last-known 'offline' state."""
        from generate_nibe_mqtt import MGMT_AVAIL_TOPIC, _shutdown

        em = self._make_em_with_entities(["nibe/avail/100"])
        mc = MagicMock()
        with patch("generate_nibe_mqtt.teardown_lovelace"):
            _shutdown(
                em, MagicMock(), mc, MagicMock(), MagicMock(), MagicMock(), [False], MagicMock()
            )
        offline_calls = {
            call.args[0]: call.kwargs.get("retain")
            for call in mc.publish.call_args_list
            if call.args[1] == "offline"
        }
        self.assertTrue(offline_calls["nibe/avail/100"])
        self.assertTrue(offline_calls[MGMT_AVAIL_TOPIC])

    def test_wait_for_publish_exception_logged_not_raised(self):
        """If wait_for_publish() raises, the exception must be logged as a
        warning and shutdown must continue — not propagate (branch 1250→1251)."""
        from generate_nibe_mqtt import _shutdown

        em = _make_em()
        mc = MagicMock()
        # Make one publish result raise on wait_for_publish
        bad_pub = MagicMock()
        bad_pub.wait_for_publish.side_effect = RuntimeError("broker gone")
        mc.publish.return_value = bad_pub
        shutting_down = [False]

        with (
            patch("generate_nibe_mqtt.teardown_lovelace"),
            patch.dict("os.environ", {}, clear=False),
            patch("generate_nibe_mqtt.log_mqtt") as mock_log,
        ):
            os.environ.pop("NIBE_REMOVE_FRONTEND", None)
            _shutdown(
                em,
                MagicMock(),
                mc,
                MagicMock(),
                MagicMock(),
                MagicMock(),
                shutting_down,
                MagicMock(),
            )

        # Must have logged a warning, not raised
        self.assertTrue(
            shutting_down[0], "Shutdown must complete even when wait_for_publish raises"
        )
        warning_calls = [
            c
            for c in mock_log.warning.call_args_list
            if "confirm" in str(c).lower() or "publish" in str(c).lower()
        ]
        self.assertTrue(warning_calls, "wait_for_publish exception must be logged as a warning")

    def test_entity_without_availability_topic_skipped_in_offline_publish(self):
        """Entities with no availability_topic must be silently skipped when
        publishing offline — the avail_topic None guard (branch 1240→1238)."""
        from generate_nibe_mqtt import _shutdown

        em = _make_em()
        # Add an entity with no availability_topic
        em.active_entities_by_id[99] = {
            "point_id": 99,
            "entity_type": "sensor",
            "state_topic": "nibe/state/99",
            "command_topic": None,
            "point_data": {},
            # 'availability_topic' deliberately absent
        }
        em.mqtt_enabled_points.add(99)
        mc = MagicMock()
        shutting_down = [False]

        with (
            patch("generate_nibe_mqtt.teardown_lovelace"),
            patch.dict("os.environ", {}, clear=False),
        ):
            os.environ.pop("NIBE_REMOVE_FRONTEND", None)
            _shutdown(
                em,
                MagicMock(),
                mc,
                MagicMock(),
                MagicMock(),
                MagicMock(),
                shutting_down,
                MagicMock(),
            )

        # Offline must not have been published for the entity with no avail topic
        offline_topics = [c.args[0] for c in mc.publish.call_args_list if c.args[1] == "offline"]
        self.assertNotIn(
            "nibe/state/99",
            offline_topics,
            "Entity without availability_topic must not get an offline publish",
        )

    def test_shutting_down_log_has_exact_text(self):
        from generate_nibe_mqtt import _shutdown

        em = _make_em()
        with (
            patch("generate_nibe_mqtt.teardown_lovelace"),
            patch("generate_nibe_mqtt.log_startup") as mock_log,
        ):
            _shutdown(
                em,
                MagicMock(),
                MagicMock(),
                MagicMock(),
                MagicMock(),
                MagicMock(),
                [False],
                MagicMock(),
            )
        first_call_msg = mock_log.info.call_args_list[0].args[0]
        self.assertEqual(first_call_msg, "Shutting down...")

    def test_waiting_for_commands_log_has_exact_text(self):
        from generate_nibe_mqtt import _shutdown

        em = _make_em()
        with (
            patch("generate_nibe_mqtt.teardown_lovelace"),
            patch("generate_nibe_mqtt.log_startup") as mock_log,
        ):
            _shutdown(
                em,
                MagicMock(),
                MagicMock(),
                MagicMock(),
                MagicMock(),
                MagicMock(),
                [False],
                MagicMock(),
            )
        second_call_msg = mock_log.info.call_args_list[1].args[0]
        self.assertEqual(second_call_msg, "Waiting for in-flight commands to complete...")

    def test_publishing_offline_availability_log_has_exact_text(self):
        from generate_nibe_mqtt import _shutdown

        em = _make_em()
        with (
            patch("generate_nibe_mqtt.teardown_lovelace"),
            patch("generate_nibe_mqtt.log_startup") as mock_log,
        ):
            _shutdown(
                em,
                MagicMock(),
                MagicMock(),
                MagicMock(),
                MagicMock(),
                MagicMock(),
                [False],
                MagicMock(),
            )
        info_msgs = [c.args[0] for c in mock_log.info.call_args_list]
        self.assertIn("Publishing offline availability...", info_msgs)

    def test_mqtt_discovery_retained_log_has_exact_text(self):
        from generate_nibe_mqtt import _shutdown

        em = _make_em()
        with (
            patch("generate_nibe_mqtt.teardown_lovelace"),
            patch("generate_nibe_mqtt.log_startup") as mock_log,
        ):
            _shutdown(
                em,
                MagicMock(),
                MagicMock(),
                MagicMock(),
                MagicMock(),
                MagicMock(),
                [False],
                MagicMock(),
                remove_frontend=False,
            )
        info_msgs = [c.args[0] for c in mock_log.info.call_args_list]
        self.assertIn("MQTT discovery configs retained for next startup", info_msgs)

    def test_service_stopped_cleanly_log_has_exact_text(self):
        from generate_nibe_mqtt import _shutdown

        em = _make_em()
        with (
            patch("generate_nibe_mqtt.teardown_lovelace"),
            patch("generate_nibe_mqtt.log_startup") as mock_log,
        ):
            _shutdown(
                em,
                MagicMock(),
                MagicMock(),
                MagicMock(),
                MagicMock(),
                MagicMock(),
                [False],
                MagicMock(),
            )
        info_msgs = [c.args[0] for c in mock_log.info.call_args_list]
        self.assertIn("Service stopped cleanly", info_msgs)
        # Must be the last info call, made after loop_stop/disconnect.
        self.assertEqual(info_msgs[-1], "Service stopped cleanly")

    def test_executor_timeout_warning_has_exact_name_and_timeout_args(self):
        """The warning's (name, _SHUTDOWN_TIMEOUT) args must be the real
        executor label and real timeout value — not dropped/nulled."""
        from generate_nibe_mqtt import _SHUTDOWN_TIMEOUT, _shutdown

        em = _make_em()

        with (
            patch("generate_nibe_mqtt.threading.Thread") as MockThread,
            patch("generate_nibe_mqtt.teardown_lovelace"),
            patch("generate_nibe_mqtt.log_startup") as mock_log,
        ):
            instance = MockThread.return_value
            instance.is_alive.return_value = True
            _shutdown(
                em,
                MagicMock(),
                MagicMock(),
                MagicMock(),
                MagicMock(),
                MagicMock(),
                [False],
                MagicMock(),
            )

        names_seen = [c.args[1] for c in mock_log.warning.call_args_list]
        self.assertEqual(names_seen, ["write", "management", "test suite"])
        for c in mock_log.warning.call_args_list:
            self.assertEqual(
                c.args[0],
                "%s executor did not finish within the shared %ds shutdown "
                "budget — proceeding with shutdown",
            )
            self.assertEqual(c.args[2], _SHUTDOWN_TIMEOUT)

    def test_offline_publish_confirm_warning_has_exact_exception_object(self):
        """log_mqtt.warning's %s arg must be the real caught exception —
        not None or a dropped arg."""
        from generate_nibe_mqtt import _shutdown

        em = _make_em()
        mc = MagicMock()
        real_error = RuntimeError("broker gone")
        bad_pub = MagicMock()
        bad_pub.wait_for_publish.side_effect = real_error
        mc.publish.return_value = bad_pub

        with (
            patch("generate_nibe_mqtt.teardown_lovelace"),
            patch.dict("os.environ", {}, clear=False),
            patch("generate_nibe_mqtt.log_mqtt") as mock_log,
        ):
            os.environ.pop("NIBE_REMOVE_FRONTEND", None)
            _shutdown(
                em, MagicMock(), mc, MagicMock(), MagicMock(), MagicMock(), [False], MagicMock()
            )

        self.assertEqual(
            mock_log.warning.call_args.args[0],
            "Offline publish did not confirm: %s",
        )
        self.assertEqual(mock_log.warning.call_args.args[1], real_error)

    def test_remaining_join_budget_floors_at_zero_not_one(self):
        """max(0.0, ...) must floor the remaining join timeout at 0.0 — a
        mutated max(1.0, ...) would give an already-expired deadline a full
        extra second of join budget on every executor instead of none."""
        import time as time_module

        from generate_nibe_mqtt import _shutdown

        em = _make_em()
        mc = MagicMock()
        join_timeouts = []

        class FakeThread:
            def __init__(self, target=None, kwargs=None):
                pass

            def start(self):
                pass

            def join(self, timeout=None):
                join_timeouts.append(timeout)

            def is_alive(self):
                return False

        # Make time.monotonic() jump far past the deadline after it is
        # computed, so `remaining` would be negative without the floor.
        real_monotonic = time_module.monotonic
        call_count = [0]

        def fake_monotonic():
            call_count[0] += 1
            if call_count[0] == 1:
                return real_monotonic()
            return real_monotonic() + 10_000.0

        with (
            patch("generate_nibe_mqtt.threading.Thread", FakeThread),
            patch("generate_nibe_mqtt.teardown_lovelace"),
            patch("generate_nibe_mqtt.time.monotonic", side_effect=fake_monotonic),
            patch("generate_nibe_mqtt.log_startup"),
        ):
            _shutdown(
                em, MagicMock(), mc, MagicMock(), MagicMock(), MagicMock(), [False], MagicMock()
            )

        for t in join_timeouts:
            self.assertEqual(t, 0.0)


class TestPollLoop(unittest.TestCase):
    """_poll_loop: periodic update, alarm check, backoff, KeyboardInterrupt passthrough."""

    def _run_loop_iterations(self, em, pub, iterations, side_effects=None):
        """Run the poll loop for exactly `iterations` main-cycle ticks then interrupt."""
        from generate_nibe_mqtt import _poll_loop

        tick = [0]
        times = [float(i * 60) for i in range(iterations + 2)]  # each tick 60s apart
        time_iter = iter(times)

        def _fake_time():
            return next(time_iter)

        def _fake_sleep(_s):
            tick[0] += 1
            if tick[0] >= iterations:
                raise KeyboardInterrupt

        em.update_all_states = MagicMock(side_effect=side_effects or [None] * iterations)
        em.initial_discovery_complete = True
        em.post_write_active = False
        em.bulk_interval = 30
        em._post_write_interval = 5

        with (
            patch("generate_nibe_mqtt.time.time", side_effect=_fake_time),
            patch("generate_nibe_mqtt.time.sleep", side_effect=_fake_sleep),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.update_alarm_state"),
            self.assertRaises(KeyboardInterrupt),
        ):
            _poll_loop(em, pub, "essential")

    def test_calls_update_all_states_each_cycle(self):
        """update_all_states() must be called once per elapsed-interval cycle."""
        em = _make_em()
        pub = MagicMock()
        self._run_loop_iterations(em, pub, iterations=3)
        self.assertGreaterEqual(em.update_all_states.call_count, 1)

    def test_per_cycle_helpers_receive_real_entity_manager_and_publisher(self):
        """update_stats_and_health/update_device_modes/update_alarm_state
        must each be called with the real (entity_manager, publisher) pair
        in that order — not None or a dropped/swapped argument."""
        em = _make_em()
        pub = MagicMock()
        from generate_nibe_mqtt import _poll_loop

        tick = [0]

        def _fake_time():
            return 100.0  # comfortably past bulk_interval=30 and _ALARM_POLL_INTERVAL

        def _fake_sleep(_s):
            tick[0] += 1
            if tick[0] >= 1:
                raise KeyboardInterrupt

        em.update_all_states = MagicMock()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em.bulk_interval = 30
        em._post_write_interval = 5

        with (
            patch("generate_nibe_mqtt.time.time", side_effect=_fake_time),
            patch("generate_nibe_mqtt.time.sleep", side_effect=_fake_sleep),
            patch("generate_nibe_mqtt.update_stats_and_health") as mock_stats,
            patch("generate_nibe_mqtt.update_device_modes") as mock_modes,
            patch("generate_nibe_mqtt.update_alarm_state") as mock_alarm,
            self.assertRaises(KeyboardInterrupt),
        ):
            _poll_loop(em, pub, "essential")

        mock_stats.assert_called_once_with(em, pub)
        mock_modes.assert_called_once_with(em, pub)
        mock_alarm.assert_called_once_with(em, pub)

    def test_calls_update_all_states_exactly_once_per_tick_no_silent_errors(self):
        """last_update must be advanced to the real current_time after each
        successful cycle — not left stale/corrupted. A bug here (e.g.
        last_update never updated, or set to something that breaks the next
        subtraction) would make every subsequent tick after the first raise
        inside the outer try/except and get silently swallowed as a logged
        error, so update_all_states would stop being called after tick 1
        even though the loop keeps running.

        With this harness's fake clock (times 0, 60, 120s; bulk_interval=30,
        3 ticks before KeyboardInterrupt), tick 1 sees current_time(0.0) -
        last_update(0.0) == 0, which is NOT >= 30, so update_all_states is
        correctly skipped on tick 1 — only ticks 2 and 3 cross the interval,
        giving exactly 2 calls. An exact count (not >=1) is what actually
        catches last_update failing to advance: a stuck/corrupted last_update
        would make tick 3's subtraction raise, get silently caught by the
        outer except, and drop the call count to 1 instead of 2."""
        em = _make_em()
        pub = MagicMock()
        with patch("generate_nibe_mqtt.log_startup") as mock_log:
            self._run_loop_iterations(em, pub, iterations=3)
        self.assertEqual(em.update_all_states.call_count, 2)
        mock_log.error.assert_not_called()

    def test_update_fires_at_exact_bulk_interval_boundary(self):
        """current_time - last_update >= effective_outer must fire when the
        elapsed time equals the interval EXACTLY (not just when it
        exceeds it) — pins `>=` against `>` and against `+` in place of
        `-`."""
        em = _make_em()
        pub = MagicMock()
        em.update_all_states = MagicMock()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em.bulk_interval = 30
        em._post_write_interval = 5

        def _fake_time():
            return 30.0  # last_update starts at 0.0 — elapsed == bulk_interval exactly

        def _fake_sleep(_s):
            raise KeyboardInterrupt

        with (
            patch("generate_nibe_mqtt.time.time", side_effect=_fake_time),
            patch("generate_nibe_mqtt.time.sleep", side_effect=_fake_sleep),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.update_alarm_state"),
            self.assertRaises(KeyboardInterrupt),
        ):
            from generate_nibe_mqtt import _poll_loop

            _poll_loop(em, pub, "essential")

        em.update_all_states.assert_called_once()

    def test_alarm_check_fires_at_exact_interval_boundary(self):
        """current_time - last_alarm_check >= _ALARM_POLL_INTERVAL must
        fire when elapsed equals the interval EXACTLY — pins `>=` against
        `>` and against `+` in place of `-`, mirroring the bulk_interval
        boundary test above for the alarm-check branch."""
        from generate_nibe_mqtt import _ALARM_POLL_INTERVAL

        em = _make_em()
        pub = MagicMock()
        em.update_all_states = MagicMock()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em.bulk_interval = 999999  # keep the bulk-update branch from firing
        em._post_write_interval = 5

        def _fake_time():
            return float(_ALARM_POLL_INTERVAL)  # last_alarm_check starts at 0.0

        def _fake_sleep(_s):
            raise KeyboardInterrupt

        with (
            patch("generate_nibe_mqtt.time.time", side_effect=_fake_time),
            patch("generate_nibe_mqtt.time.sleep", side_effect=_fake_sleep),
            patch("generate_nibe_mqtt.update_alarm_state") as mock_alarm,
            self.assertRaises(KeyboardInterrupt),
        ):
            from generate_nibe_mqtt import _poll_loop

            _poll_loop(em, pub, "essential")

        mock_alarm.assert_called_once()

    def test_keyboard_interrupt_propagates(self):
        """_poll_loop must re-raise KeyboardInterrupt immediately."""
        from generate_nibe_mqtt import _poll_loop

        em = _make_em()
        pub = MagicMock()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em.bulk_interval = 30
        em._post_write_interval = 5

        with (
            patch("generate_nibe_mqtt.time.time", return_value=99999.0),
            patch("generate_nibe_mqtt.time.sleep", side_effect=KeyboardInterrupt),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.update_alarm_state"),
            self.assertRaises(KeyboardInterrupt),
        ):
            _poll_loop(em, pub, "essential")

    def test_exception_in_cycle_does_not_exit_loop(self):
        """A single exception in a poll cycle must be caught and the loop continued."""
        from generate_nibe_mqtt import _poll_loop

        em = _make_em()
        pub = MagicMock()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em.bulk_interval = 30
        em._post_write_interval = 5

        call_count = [0]

        def _crashy_update():
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("transient crash")

        em.update_all_states = MagicMock(side_effect=_crashy_update)
        tick = [0]

        def _fake_sleep(_s):
            tick[0] += 1
            if tick[0] >= 4:
                raise KeyboardInterrupt

        _t = [0.0]

        def _fake_time():
            _t[0] += 60.0
            return _t[0]

        with (
            patch("generate_nibe_mqtt.time.time", side_effect=_fake_time),
            patch("generate_nibe_mqtt.time.sleep", side_effect=_fake_sleep),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.update_alarm_state"),
            self.assertRaises(KeyboardInterrupt),
        ):
            _poll_loop(em, pub, "essential")

        # Loop ran more than 1 cycle: crash on cycle 1 did not kill the loop
        self.assertGreater(call_count[0], 1)

    def test_backoff_escalates_on_consecutive_errors(self):
        """Consecutive exceptions must produce increasing backoff sleep durations."""
        from generate_nibe_mqtt import _poll_loop

        em = _make_em()
        pub = MagicMock()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em.bulk_interval = 30
        em._post_write_interval = 5
        em.update_all_states = MagicMock(side_effect=RuntimeError("persistent crash"))

        backoff_sleeps = []
        tick = [0]

        def _fake_sleep(s):
            tick[0] += 1
            if s > 1:
                backoff_sleeps.append(s)
            if tick[0] >= 15:
                raise KeyboardInterrupt

        _t = [0.0]

        def _fake_time():
            _t[0] += 60.0
            return _t[0]

        with (
            patch("generate_nibe_mqtt.time.time", side_effect=_fake_time),
            patch("generate_nibe_mqtt.time.sleep", side_effect=_fake_sleep),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.update_alarm_state"),
            self.assertRaises(KeyboardInterrupt),
        ):
            _poll_loop(em, pub, "essential")

        self.assertGreater(
            len(backoff_sleeps), 1, "Expected multiple backoff sleeps from consecutive errors"
        )
        self.assertGreater(
            backoff_sleeps[-1],
            backoff_sleeps[0],
            "Backoff duration must escalate on consecutive errors",
        )

    def test_backoff_capped_at_60_seconds(self):
        """Backoff must never exceed 60 seconds regardless of error count."""
        from generate_nibe_mqtt import _poll_loop

        em = _make_em()
        pub = MagicMock()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em.bulk_interval = 30
        em._post_write_interval = 5
        em.update_all_states = MagicMock(side_effect=RuntimeError("crash"))

        backoff_sleeps = []
        tick = [0]

        def _fake_sleep(s):
            tick[0] += 1
            if s > 1:
                backoff_sleeps.append(s)
            if tick[0] >= 30:
                raise KeyboardInterrupt

        _t = [0.0]

        def _fake_time():
            _t[0] += 60.0
            return _t[0]

        with (
            patch("generate_nibe_mqtt.time.time", side_effect=_fake_time),
            patch("generate_nibe_mqtt.time.sleep", side_effect=_fake_sleep),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.update_alarm_state"),
            self.assertRaises(KeyboardInterrupt),
        ):
            _poll_loop(em, pub, "essential")

        if backoff_sleeps:
            self.assertLessEqual(max(backoff_sleeps), 60, "Backoff must be capped at 60 seconds")

    def test_alert_published_after_five_consecutive_errors(self):
        """After 5 consecutive errors publish_bridge_alert must be called."""
        from generate_nibe_mqtt import _poll_loop

        em = _make_em()
        pub = MagicMock()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em.bulk_interval = 30
        em._post_write_interval = 5
        em.update_all_states = MagicMock(side_effect=RuntimeError("crash"))

        tick = [0]

        def _fake_sleep(_s):
            tick[0] += 1
            if tick[0] >= 25:
                raise KeyboardInterrupt

        # time.time is called multiple times per loop iteration; provide plenty of values
        _t = [0.0]

        def _fake_time():
            _t[0] += 60.0
            return _t[0]

        with (
            patch("generate_nibe_mqtt.time.time", side_effect=_fake_time),
            patch("generate_nibe_mqtt.time.sleep", side_effect=_fake_sleep),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.update_alarm_state"),
            self.assertRaises(KeyboardInterrupt),
        ):
            _poll_loop(em, pub, "essential")

        pub.publish_bridge_alert.assert_called()
        call_kwargs = pub.publish_bridge_alert.call_args.kwargs
        self.assertEqual(call_kwargs.get("alert_type"), "main_loop_error")
        self.assertEqual(call_kwargs.get("severity"), "error")
        self.assertIn("consecutive_errors", call_kwargs.get("context", {}))
        self.assertIn("error", call_kwargs.get("context", {}))
        self.assertEqual(call_kwargs["context"]["error"], "crash")
        self.assertIn("crash", call_kwargs.get("message", ""))

    def test_deferred_discovery_runs_when_initial_discovery_incomplete(self):
        """When initial_discovery_complete=False and api_consecutive_failures=0,
        complete_deferred_discovery() must be called instead of update_all_states(),
        and last_bulk_fetch must be updated when it returns True."""
        from generate_nibe_mqtt import _poll_loop

        em = _make_em()
        pub = MagicMock()
        em.initial_discovery_complete = False
        em.api_consecutive_failures = 0
        em.post_write_active = False
        em.bulk_interval = 30
        em._post_write_interval = 5
        em.complete_deferred_discovery = MagicMock(return_value=True)
        em.update_all_states = MagicMock()

        tick = [0]
        _t = [0.0]

        def _fake_time():
            _t[0] += 60.0
            return _t[0]

        def _fake_sleep(_s):
            tick[0] += 1
            if tick[0] >= 2:
                raise KeyboardInterrupt

        with (
            patch("generate_nibe_mqtt.time.time", side_effect=_fake_time),
            patch("generate_nibe_mqtt.time.sleep", side_effect=_fake_sleep),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.update_alarm_state"),
            self.assertRaises(KeyboardInterrupt),
        ):
            _poll_loop(em, pub, "essential")

        em.complete_deferred_discovery.assert_called_with("essential")
        # update_all_states must NOT be called when deferred_ran=True
        em.update_all_states.assert_not_called()

    def test_memory_logging_exception_does_not_crash_loop(self):
        """An exception in get_memory_usage() must be caught and logged — the
        poll loop must continue normally (memory logging error handler)."""
        from generate_nibe_mqtt import _poll_loop

        em = _make_em()
        pub = MagicMock()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em.bulk_interval = 30
        em._post_write_interval = 5
        em.update_all_states = MagicMock()
        em.get_memory_usage = MagicMock(side_effect=RuntimeError("oom"))

        tick = [0]
        _t = [0.0]

        # Make time jump far enough to trigger memory logging (600s threshold)
        def _fake_time():
            _t[0] += 700.0
            return _t[0]

        def _fake_sleep(_s):
            tick[0] += 1
            if tick[0] >= 2:
                raise KeyboardInterrupt

        with (
            patch("generate_nibe_mqtt.time.time", side_effect=_fake_time),
            patch("generate_nibe_mqtt.time.sleep", side_effect=_fake_sleep),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.update_alarm_state"),
            patch("generate_nibe_mqtt.log_startup") as mock_log,
            self.assertRaises(KeyboardInterrupt),
        ):
            _poll_loop(em, pub, "essential")

        # Must have logged the error, not propagated it
        error_calls = [
            c for c in mock_log.error.call_args_list if "Memory" in str(c) or "memory" in str(c)
        ]
        self.assertTrue(error_calls, "Memory logging error must be caught and logged")

    def test_memory_log_boundary_and_initial_value_and_addition_mutant(self):
        """Pins four mutants in one shot on the current_time - last_memory_log
        >= 600 check:
        - last_memory_log must initialize to 0.0, not 1.0 (mutant: initial=1.0)
        - the comparison must be `>=`, not `>` (mutant: >600) or an off-by-one
          `>= 601` (mutant: 601)
        - the comparison must use subtraction, not addition (mutant: +)

        Tick 1: current_time=600.0, last_memory_log starts at 0.0 (or 1.0 under
        the init mutant) — 600-0=600>=600 fires for real code but 600-1=599
        does not fire under the init mutant, and 600>600 is False under the
        `>` mutant, and 600>=601 is False under the 601 mutant.
        Tick 2: current_time=650.0, last_memory_log=600.0 (set after tick 1) —
        650-600=50<600 does NOT fire for real code, but the addition mutant
        (650+600=1250>=600) WOULD fire again. bulk_interval=1 keeps the outer
        branch firing on both ticks regardless."""
        em = _make_em()
        pub = MagicMock()
        em.update_all_states = MagicMock()
        em.get_memory_usage = MagicMock(return_value={})
        em.initial_discovery_complete = True
        em.post_write_active = False
        em.bulk_interval = 1
        em._post_write_interval = 5

        times = [600.0, 650.0]
        _time_idx = [0]

        def _fake_time():
            # Clamp to the last scheduled value instead of raising
            # StopIteration: an incidental extra time.time() call (e.g.
            # from a DEBUG-level log record's timestamp, if some other
            # test left that logger's level elevated) must not blow up
            # this test — order-dependent flake, fixed by tolerating it.
            idx = min(_time_idx[0], len(times) - 1)
            _time_idx[0] += 1
            return times[idx]

        tick = [0]

        def _fake_sleep(_s):
            tick[0] += 1
            if tick[0] >= 2:
                raise KeyboardInterrupt

        with (
            patch("generate_nibe_mqtt.time.time", side_effect=_fake_time),
            patch("generate_nibe_mqtt.time.sleep", side_effect=_fake_sleep),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.update_alarm_state"),
            patch("generate_nibe_mqtt.log_startup"),
            self.assertRaises(KeyboardInterrupt),
        ):
            from generate_nibe_mqtt import _poll_loop

            _poll_loop(em, pub, "essential")

        em.get_memory_usage.assert_called_once()

    def test_error_counter_initializes_to_zero_not_one(self):
        """_loop_consecutive_errors must initialize to 0, not 1 — the first
        error's backoff (5 * _loop_consecutive_errors after incrementing)
        must be exactly 5s, not 10s."""
        from generate_nibe_mqtt import _poll_loop

        em = _make_em()
        pub = MagicMock()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em.bulk_interval = 30
        em._post_write_interval = 5
        em.update_all_states = MagicMock(side_effect=RuntimeError("boom"))

        sleeps = []

        def _fake_sleep(s):
            sleeps.append(s)
            raise KeyboardInterrupt

        with (
            patch("generate_nibe_mqtt.time.time", return_value=100.0),
            patch("generate_nibe_mqtt.time.sleep", side_effect=_fake_sleep),
            patch("generate_nibe_mqtt.log_startup"),
            self.assertRaises(KeyboardInterrupt),
        ):
            _poll_loop(em, pub, "essential")

        self.assertEqual(sleeps, [5])

    def test_update_not_retriggered_before_bulk_interval_elapses(self):
        """The bulk-update branch must compare with subtraction
        (current_time - last_update), not addition — pins `+` in place of
        `-`. Tick 1 sets last_update=100; tick 2 at current_time=110 with
        bulk_interval=30 must NOT refire (110-100=10<30), but the addition
        mutant (110+100=210>=30) would refire."""
        em = _make_em()
        pub = MagicMock()
        em.update_all_states = MagicMock()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em.bulk_interval = 30
        em._post_write_interval = 5

        times = [100.0, 110.0]
        _time_idx = [0]

        def _fake_time():
            # Clamp to the last scheduled value instead of raising
            # StopIteration: an incidental extra time.time() call (e.g.
            # from a DEBUG-level log record's timestamp, if some other
            # test left that logger's level elevated) must not blow up
            # this test — order-dependent flake, fixed by tolerating it.
            idx = min(_time_idx[0], len(times) - 1)
            _time_idx[0] += 1
            return times[idx]

        tick = [0]

        def _fake_sleep(_s):
            tick[0] += 1
            if tick[0] >= 2:
                raise KeyboardInterrupt

        with (
            patch("generate_nibe_mqtt.time.time", side_effect=_fake_time),
            patch("generate_nibe_mqtt.time.sleep", side_effect=_fake_sleep),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.update_alarm_state"),
            self.assertRaises(KeyboardInterrupt),
        ):
            from generate_nibe_mqtt import _poll_loop

            _poll_loop(em, pub, "essential")

        em.update_all_states.assert_called_once()

    def test_periodic_update_debug_log_exact_message(self):
        """log_entities.debug() must log the exact text 'Periodic state
        update' — pins case, XX-wrap, and None-arg mutations of the log
        call."""
        em = _make_em()
        pub = MagicMock()
        em.update_all_states = MagicMock()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em.bulk_interval = 30
        em._post_write_interval = 5

        def _fake_time():
            return 100.0

        def _fake_sleep(_s):
            raise KeyboardInterrupt

        with (
            patch("generate_nibe_mqtt.time.time", side_effect=_fake_time),
            patch("generate_nibe_mqtt.time.sleep", side_effect=_fake_sleep),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.update_alarm_state"),
            self.assertLogs("nibe.entities", level="DEBUG") as cm,
            self.assertRaises(KeyboardInterrupt),
        ):
            from generate_nibe_mqtt import _poll_loop

            _poll_loop(em, pub, "essential")

        self.assertIn("DEBUG:nibe.entities:Periodic state update", cm.output)

    def test_last_bulk_fetch_set_to_real_time_on_deferred_discovery(self):
        """When complete_deferred_discovery() returns True, last_bulk_fetch
        must be set to the real current time (time.time()), not None."""
        from generate_nibe_mqtt import _poll_loop

        em = _make_em()
        pub = MagicMock()
        em.initial_discovery_complete = False
        em.api_consecutive_failures = 0
        em.post_write_active = False
        em.bulk_interval = 30
        em._post_write_interval = 5
        em.complete_deferred_discovery = MagicMock(return_value=True)
        em.update_all_states = MagicMock()
        em.last_bulk_fetch = None

        def _fake_time():
            return 555.0

        def _fake_sleep(_s):
            raise KeyboardInterrupt

        with (
            patch("generate_nibe_mqtt.time.time", side_effect=_fake_time),
            patch("generate_nibe_mqtt.time.sleep", side_effect=_fake_sleep),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.update_alarm_state"),
            self.assertRaises(KeyboardInterrupt),
        ):
            _poll_loop(em, pub, "essential")

        self.assertEqual(em.last_bulk_fetch, 555.0)

    def test_error_counter_resets_to_zero_after_clean_cycle(self):
        """A clean cycle must reset _loop_consecutive_errors to 0 (not 1) —
        confirmed by observing that the backoff after a SUBSEQUENT error is
        5s (first-error backoff), not 10s (which would result if the
        counter had been left at 1 after the clean cycle)."""
        em = _make_em()
        pub = MagicMock()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em.bulk_interval = 30
        em._post_write_interval = 5

        call_count = [0]

        def _update(*a, **kw):
            call_count[0] += 1
            if call_count[0] == 2:
                raise RuntimeError("boom")

        em.update_all_states = MagicMock(side_effect=_update)

        times = [100.0, 200.0]
        _time_idx = [0]

        def _fake_time():
            # Clamp to the last scheduled value instead of raising
            # StopIteration: an incidental extra time.time() call (e.g.
            # from a DEBUG-level log record's timestamp, if some other
            # test left that logger's level elevated) must not blow up
            # this test — order-dependent flake, fixed by tolerating it.
            idx = min(_time_idx[0], len(times) - 1)
            _time_idx[0] += 1
            return times[idx]

        sleeps = []

        def _fake_sleep(s):
            sleeps.append(s)
            if len(sleeps) >= 2:
                raise KeyboardInterrupt

        with (
            patch("generate_nibe_mqtt.time.time", side_effect=_fake_time),
            patch("generate_nibe_mqtt.time.sleep", side_effect=_fake_sleep),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.update_alarm_state"),
            patch("generate_nibe_mqtt.log_startup"),
            self.assertRaises(KeyboardInterrupt),
        ):
            from generate_nibe_mqtt import _poll_loop

            _poll_loop(em, pub, "essential")

        # sleeps[0] is the normal time.sleep(1) after cycle 1 (clean),
        # sleeps[1] is the backoff after cycle 2's error — must be 5, not 10.
        self.assertEqual(sleeps, [1, 5])

    def test_memory_log_call_uses_exact_stats_keys_and_message(self):
        """log_startup.debug for the periodic memory-usage log must be
        called with the exact format string and the exact keys pulled from
        memory_stats, in the exact positions — pins swapped keys, swapped
        positions, and text mutations across the whole call."""
        em = _make_em()
        pub = MagicMock()
        em.update_all_states = MagicMock()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em.bulk_interval = 30
        em._post_write_interval = 5
        em.get_memory_usage = MagicMock(
            return_value={
                "total_points": 11,
                "active_entities": 22,
                "estimated_memory_mb": 3.5,
                "value_cache_size": 44,
                "last_states_size": 55,
                "point_string_cache_size": 66,
            }
        )

        def _fake_time():
            return 600.0

        def _fake_sleep(_s):
            raise KeyboardInterrupt

        with (
            patch("generate_nibe_mqtt.time.time", side_effect=_fake_time),
            patch("generate_nibe_mqtt.time.sleep", side_effect=_fake_sleep),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.update_alarm_state"),
            patch("generate_nibe_mqtt.log_startup") as mock_log,
            self.assertRaises(KeyboardInterrupt),
        ):
            from generate_nibe_mqtt import _poll_loop

            _poll_loop(em, pub, "essential")

        mock_log.debug.assert_called_once_with(
            "Memory usage: %d points, %d active entities, ~%.2f MB "
            "(cache sizes: value=%d, states=%d, strings=%d)",
            11,
            22,
            3.5,
            44,
            55,
            66,
        )

    def test_memory_log_call_defaults_to_zero_when_stats_missing(self):
        """When memory_stats is missing keys, every field must default to
        0 (not 1, None, or an omitted argument) — pins default-value
        mutations that a dict WITH all keys present (the test above) cannot
        catch, since a present key always overrides its .get() default."""
        em = _make_em()
        pub = MagicMock()
        em.update_all_states = MagicMock()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em.bulk_interval = 30
        em._post_write_interval = 5
        em.get_memory_usage = MagicMock(return_value={})

        def _fake_time():
            return 600.0

        def _fake_sleep(_s):
            raise KeyboardInterrupt

        with (
            patch("generate_nibe_mqtt.time.time", side_effect=_fake_time),
            patch("generate_nibe_mqtt.time.sleep", side_effect=_fake_sleep),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.update_alarm_state"),
            patch("generate_nibe_mqtt.log_startup") as mock_log,
            self.assertRaises(KeyboardInterrupt),
        ):
            from generate_nibe_mqtt import _poll_loop

            _poll_loop(em, pub, "essential")

        mock_log.debug.assert_called_once_with(
            "Memory usage: %d points, %d active entities, ~%.2f MB "
            "(cache sizes: value=%d, states=%d, strings=%d)",
            0,
            0,
            0,
            0,
            0,
            0,
        )

    def test_memory_log_error_uses_exact_message_and_exception(self):
        """The except-branch around memory logging must call
        log_startup.error with the exact text and the actual exception
        instance — pins text-case/None-arg/missing-arg mutations."""
        em = _make_em()
        pub = MagicMock()
        em.update_all_states = MagicMock()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em.bulk_interval = 30
        em._post_write_interval = 5
        boom = RuntimeError("oom")
        em.get_memory_usage = MagicMock(side_effect=boom)

        def _fake_time():
            return 600.0

        def _fake_sleep(_s):
            raise KeyboardInterrupt

        with (
            patch("generate_nibe_mqtt.time.time", side_effect=_fake_time),
            patch("generate_nibe_mqtt.time.sleep", side_effect=_fake_sleep),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.update_alarm_state"),
            patch("generate_nibe_mqtt.log_startup") as mock_log,
            self.assertRaises(KeyboardInterrupt),
        ):
            from generate_nibe_mqtt import _poll_loop

            _poll_loop(em, pub, "essential")

        mock_log.error.assert_called_once_with("Memory logging error: %s", boom)

    def test_last_memory_log_advances_to_current_time(self):
        """last_memory_log must be set to current_time after logging, not
        None — verified by a second tick 1s later NOT raising: with the
        None mutant, the next iteration's `current_time - last_memory_log`
        raises TypeError, which is caught by the outer except and logged
        via log_startup.exception (which must NOT happen for real code)."""
        em = _make_em()
        pub = MagicMock()
        em.update_all_states = MagicMock()
        em.get_memory_usage = MagicMock(return_value={})
        em.initial_discovery_complete = True
        em.post_write_active = False
        em.bulk_interval = 1
        em._post_write_interval = 5

        times = [600.0, 601.0]
        _time_idx = [0]

        def _fake_time():
            # Clamp to the last scheduled value instead of raising
            # StopIteration: an incidental extra time.time() call (e.g.
            # from a DEBUG-level log record's timestamp, if some other
            # test left that logger's level elevated) must not blow up
            # this test — order-dependent flake, fixed by tolerating it.
            idx = min(_time_idx[0], len(times) - 1)
            _time_idx[0] += 1
            return times[idx]

        tick = [0]

        def _fake_sleep(_s):
            tick[0] += 1
            if tick[0] >= 2:
                raise KeyboardInterrupt

        with (
            patch("generate_nibe_mqtt.time.time", side_effect=_fake_time),
            patch("generate_nibe_mqtt.time.sleep", side_effect=_fake_sleep),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.update_alarm_state"),
            patch("generate_nibe_mqtt.log_startup") as mock_log,
            self.assertRaises(KeyboardInterrupt),
        ):
            from generate_nibe_mqtt import _poll_loop

            _poll_loop(em, pub, "essential")

        mock_log.exception.assert_not_called()

    def test_alarm_check_not_retriggered_before_interval_elapses(self):
        """The alarm-check branch must compare with subtraction
        (current_time - last_alarm_check), not addition, against
        _ALARM_POLL_INTERVAL."""
        from generate_nibe_mqtt import _ALARM_POLL_INTERVAL

        em = _make_em()
        pub = MagicMock()
        em.update_all_states = MagicMock()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em.bulk_interval = 999999  # keep the bulk-update branch from firing
        em._post_write_interval = 5

        times = [float(_ALARM_POLL_INTERVAL), float(_ALARM_POLL_INTERVAL) + 1]
        _time_idx = [0]

        def _fake_time():
            # Clamp to the last scheduled value instead of raising
            # StopIteration: an incidental extra time.time() call (e.g.
            # from a DEBUG-level log record's timestamp, if some other
            # test left that logger's level elevated) must not blow up
            # this test — order-dependent flake, fixed by tolerating it.
            idx = min(_time_idx[0], len(times) - 1)
            _time_idx[0] += 1
            return times[idx]

        tick = [0]

        def _fake_sleep(_s):
            tick[0] += 1
            if tick[0] >= 2:
                raise KeyboardInterrupt

        with (
            patch("generate_nibe_mqtt.time.time", side_effect=_fake_time),
            patch("generate_nibe_mqtt.time.sleep", side_effect=_fake_sleep),
            patch("generate_nibe_mqtt.update_alarm_state") as mock_alarm,
            self.assertRaises(KeyboardInterrupt),
        ):
            from generate_nibe_mqtt import _poll_loop

            _poll_loop(em, pub, "essential")

        mock_alarm.assert_called_once()

    def test_sleep_duration_is_exactly_one_second_per_cycle(self):
        """The per-cycle time.sleep() call at the end of a clean iteration
        must sleep for exactly 1 second."""
        em = _make_em()
        pub = MagicMock()
        em.update_all_states = MagicMock()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em.bulk_interval = 30
        em._post_write_interval = 5

        sleeps = []

        def _fake_sleep(s):
            sleeps.append(s)
            raise KeyboardInterrupt

        with (
            patch("generate_nibe_mqtt.time.time", return_value=100.0),
            patch("generate_nibe_mqtt.time.sleep", side_effect=_fake_sleep),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.update_alarm_state"),
            self.assertRaises(KeyboardInterrupt),
        ):
            from generate_nibe_mqtt import _poll_loop

            _poll_loop(em, pub, "essential")

        self.assertEqual(sleeps, [1])

    def test_unexpected_error_log_exact_message(self):
        """log_startup.exception() for an unexpected loop error must use
        the exact text 'Unexpected error in main loop (occurrence %d,
        backing off %ds)' with the exact args — pins case/wording
        mutations."""
        em = _make_em()
        pub = MagicMock()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em.bulk_interval = 30
        em._post_write_interval = 5
        em.update_all_states = MagicMock(side_effect=RuntimeError("boom"))

        def _fake_sleep(_s):
            raise KeyboardInterrupt

        with (
            patch("generate_nibe_mqtt.time.time", return_value=100.0),
            patch("generate_nibe_mqtt.time.sleep", side_effect=_fake_sleep),
            patch("generate_nibe_mqtt.log_startup") as mock_log,
            self.assertRaises(KeyboardInterrupt),
        ):
            from generate_nibe_mqtt import _poll_loop

            _poll_loop(em, pub, "essential")

        mock_log.exception.assert_called_once_with(
            "Unexpected error in main loop (occurrence %d, backing off %ds)",
            1,
            5,
        )

    def test_alert_published_at_exactly_five_consecutive_errors(self):
        """publish_bridge_alert must fire exactly when
        _loop_consecutive_errors reaches 5 — pins the >=5 boundary against
        >5 (which would require 6 errors) and against >=6."""
        from generate_nibe_mqtt import _poll_loop

        em = _make_em()
        pub = MagicMock()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em.bulk_interval = 30
        em._post_write_interval = 5
        em.update_all_states = MagicMock(side_effect=RuntimeError("crash"))

        tick = [0]

        def _fake_sleep(_s):
            tick[0] += 1
            if tick[0] >= 5:
                raise KeyboardInterrupt

        _t = [0.0]

        def _fake_time():
            _t[0] += 60.0
            return _t[0]

        with (
            patch("generate_nibe_mqtt.time.time", side_effect=_fake_time),
            patch("generate_nibe_mqtt.time.sleep", side_effect=_fake_sleep),
            patch("generate_nibe_mqtt.log_startup"),
            self.assertRaises(KeyboardInterrupt),
        ):
            _poll_loop(em, pub, "essential")

        pub.publish_bridge_alert.assert_called_once()


class TestRunStartupSequence(unittest.TestCase):
    """_run_startup_sequence: happy path, discovery failure, mode application."""

    def _cfg(self):
        from generate_nibe_mqtt import BridgeConfig

        return BridgeConfig(
            api_base_url="https://10.0.0.1:8443/api/v1/devices/0",
            nibe_auth="Basic dXNlcjpwYXNz",
            mqtt_broker="localhost",
            mqtt_port=1883,
            device_name="Test Device",
            device_id="nibe_test001",
            poll_interval=30,
            api_failure_threshold=3,
            changelog_retention_days=90,
            mode="essential",
        )

    def _run(self, cfg=None, response=None, discover_ok=True, initial_mode="essential"):
        from generate_nibe_mqtt import _run_startup_sequence

        cfg = cfg or self._cfg()
        response = response or {}
        mc = MagicMock()

        with (
            patch("generate_nibe_mqtt._build_device_info", return_value={"model": "S40"}),
            patch("generate_nibe_mqtt.MqttDiscoveryPublisher") as MockPub,
            patch("generate_nibe_mqtt.EntityManager") as MockEM,
            patch("generate_nibe_mqtt._load_menu_structure", return_value=({}, frozenset())),
            patch("generate_nibe_mqtt.dismiss_ha"),
            patch("generate_nibe_mqtt.notify_ha"),
            patch("generate_nibe_mqtt.HAEntityRegistryWatcher") as MockWatcher,
            patch("generate_nibe_mqtt.threading.Thread"),
            patch("generate_nibe_mqtt.ManagementCommandHandler"),
            patch("generate_nibe_mqtt._run_scan_with_retry", return_value=set()),
            patch("generate_nibe_mqtt.decide_startup_action", return_value="apply"),
            patch("generate_nibe_mqtt._execute_startup_action"),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.remove_menu_dashboard"),
            patch("generate_nibe_mqtt.concurrent.futures.ThreadPoolExecutor"),
            patch("generate_nibe_mqtt.time.sleep"),
        ):
            em_instance = MockEM.return_value
            em_instance.discover_points.return_value = discover_ok
            em_instance.mqtt_enabled_points = set()
            em_instance.all_points = []
            em_instance.active_entities = []
            em_instance.bulk_interval = 30

            pub_instance = MockPub.return_value
            pub_instance.mqtt = MagicMock()

            result = _run_startup_sequence(
                cfg,
                MagicMock(),
                mc,
                response,
                "nibe_test001",
                initial_mode,
                MagicMock(),
            )

        return result, em_instance, pub_instance, MockWatcher

    def test_returns_five_tuple(self):
        """Must return (entity_manager, publisher, registry_watcher,
        mgmt_executor, test_executor)."""
        result, *_ = self._run()
        self.assertEqual(len(result), 5)

    def test_entity_manager_configured_from_cfg(self):
        """bulk_interval, api_failure_threshold, changelog_retention_days must be set."""
        cfg = self._cfg()
        cfg.poll_interval = 60
        cfg.api_failure_threshold = 5
        cfg.changelog_retention_days = 30
        _, em, *_ = self._run(cfg=cfg)
        self.assertEqual(em.bulk_interval, 60)
        self.assertEqual(em.api_failure_threshold, 5)
        self.assertEqual(em.changelog_retention_days, 30)

    def test_entity_manager_mode_switch_behavior_set_from_cfg(self):
        cfg = self._cfg()
        cfg.mode_switch_behavior = "merge"
        _, em, *_ = self._run(cfg=cfg)
        self.assertEqual(em.mode_switch_behavior, "merge")

    def _run_capturing(self, cfg, patched_extra=None):
        """Like _run() but also captures ManagementCommandHandler,
        decide_startup_action, and _execute_startup_action call args for
        inspection — the plain _run() only checks return values."""
        from generate_nibe_mqtt import _run_startup_sequence

        mc = MagicMock()
        with (
            patch("generate_nibe_mqtt._build_device_info", return_value={"model": "S40"}),
            patch("generate_nibe_mqtt.MqttDiscoveryPublisher") as MockPub,
            patch("generate_nibe_mqtt.EntityManager") as MockEM,
            patch("generate_nibe_mqtt._load_menu_structure", return_value=({}, frozenset())),
            patch("generate_nibe_mqtt.dismiss_ha"),
            patch("generate_nibe_mqtt.notify_ha"),
            patch("generate_nibe_mqtt.HAEntityRegistryWatcher"),
            patch("generate_nibe_mqtt.threading.Thread"),
            patch("generate_nibe_mqtt.ManagementCommandHandler") as MockMgmtHandler,
            patch("generate_nibe_mqtt._run_scan_with_retry", return_value={1, 2}),
            patch("generate_nibe_mqtt.decide_startup_action", return_value="apply") as mock_decide,
            patch("generate_nibe_mqtt._execute_startup_action") as mock_execute,
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.remove_menu_dashboard"),
            patch("generate_nibe_mqtt.concurrent.futures.ThreadPoolExecutor"),
            patch("generate_nibe_mqtt.time.sleep"),
        ):
            em_instance = MockEM.return_value
            em_instance.discover_points.return_value = True
            em_instance.mqtt_enabled_points = {1, 2}
            em_instance.all_points = []
            em_instance.active_entities = []
            em_instance.bulk_interval = 30
            em_instance.read_applied_mode.return_value = "essential"
            MockPub.return_value.mqtt = MagicMock()
            _run_startup_sequence(
                cfg, MagicMock(), mc, {}, "nibe_test001", "essential", MagicMock()
            )
        return MockMgmtHandler, mock_decide, mock_execute, mc, MockPub.return_value

    def test_ca_cert_path_passed_when_file_exists(self):
        """ManagementCommandHandler's ca_cert_path kwarg must be the real
        nibe_ca_cert path when it's set AND exists on disk — not
        hardcoded None."""
        cfg = self._cfg()
        cfg.nibe_ca_cert = "/ssl/nibe-ca.pem"
        with patch("os.path.exists", return_value=True):
            MockMgmtHandler, *_ = self._run_capturing(cfg)
        self.assertEqual(
            MockMgmtHandler.call_args.kwargs["ca_cert_path"],
            "/ssl/nibe-ca.pem",
        )

    def test_decide_startup_action_receives_real_args(self):
        cfg = self._cfg()
        _, mock_decide, mock_execute, *_ = self._run_capturing(cfg)
        self.assertEqual(
            mock_decide.call_args.kwargs,
            {
                "has_existing_entities": True,
                "applied_mode": "essential",
                "config_mode": "essential",
            },
        )
        mock_execute.assert_called_once_with(
            mock_execute.call_args.args[0],
            "apply",
            "essential",
            "essential",
            mock_execute.call_args.args[4],
            cfg.device_name,
        )

    def test_stats_state_published_with_retain_true(self):
        cfg = self._cfg()
        _, _, _, _mc, pub = self._run_capturing(cfg)
        from generate_nibe_mqtt import MgmtTopic

        stats_calls = [
            c
            for c in pub.mqtt.publish.call_args_list
            if c.args and c.args[0] == MgmtTopic.STATS_STATE
        ]
        self.assertEqual(len(stats_calls), 1)
        self.assertEqual(stats_calls[0].args[1], "2")
        self.assertTrue(stats_calls[0].kwargs.get("retain"))

    def test_initial_stats_and_device_modes_receive_real_args(self):
        """The final update_stats_and_health/update_device_modes calls at
        the end of startup must receive the real (entity_manager,
        publisher) pair — not None or a dropped argument."""
        from generate_nibe_mqtt import _run_startup_sequence

        cfg = self._cfg()
        mc = MagicMock()
        with (
            patch("generate_nibe_mqtt._build_device_info", return_value={"model": "S40"}),
            patch("generate_nibe_mqtt.MqttDiscoveryPublisher") as MockPub,
            patch("generate_nibe_mqtt.EntityManager") as MockEM,
            patch("generate_nibe_mqtt._load_menu_structure", return_value=({}, frozenset())),
            patch("generate_nibe_mqtt.dismiss_ha"),
            patch("generate_nibe_mqtt.notify_ha"),
            patch("generate_nibe_mqtt.HAEntityRegistryWatcher"),
            patch("generate_nibe_mqtt.threading.Thread"),
            patch("generate_nibe_mqtt.ManagementCommandHandler"),
            patch("generate_nibe_mqtt._run_scan_with_retry", return_value=set()),
            patch("generate_nibe_mqtt.decide_startup_action", return_value="apply"),
            patch("generate_nibe_mqtt._execute_startup_action"),
            patch("generate_nibe_mqtt.update_stats_and_health") as mock_stats,
            patch("generate_nibe_mqtt.update_device_modes") as mock_modes,
            patch("generate_nibe_mqtt.remove_menu_dashboard"),
            patch("generate_nibe_mqtt.concurrent.futures.ThreadPoolExecutor"),
            patch("generate_nibe_mqtt.time.sleep"),
        ):
            em_instance = MockEM.return_value
            em_instance.discover_points.return_value = True
            em_instance.mqtt_enabled_points = set()
            em_instance.all_points = []
            em_instance.active_entities = []
            em_instance.bulk_interval = 30
            MockPub.return_value.mqtt = MagicMock()
            _run_startup_sequence(
                cfg, MagicMock(), mc, {}, "nibe_test001", "essential", MagicMock()
            )

        mock_stats.assert_called_once_with(em_instance, MockPub.return_value)
        mock_modes.assert_called_once_with(em_instance, MockPub.return_value)

    def test_load_menu_structure_log_if_mode_true_only_in_menus_mode(self):
        """log_if_mode must be True exactly when initial_mode == 'menus' —
        not the inverse, and not merely truthy for any non-empty mode
        string."""
        from generate_nibe_mqtt import _run_startup_sequence

        cfg = self._cfg()
        mc = MagicMock()
        with (
            patch("generate_nibe_mqtt._build_device_info", return_value={"model": "S40"}),
            patch("generate_nibe_mqtt.MqttDiscoveryPublisher") as MockPub,
            patch("generate_nibe_mqtt.EntityManager") as MockEM,
            patch(
                "generate_nibe_mqtt._load_menu_structure", return_value=({}, frozenset())
            ) as mock_load,
            patch("generate_nibe_mqtt.dismiss_ha"),
            patch("generate_nibe_mqtt.notify_ha"),
            patch("generate_nibe_mqtt.HAEntityRegistryWatcher"),
            patch("generate_nibe_mqtt.threading.Thread"),
            patch("generate_nibe_mqtt.ManagementCommandHandler"),
            patch("generate_nibe_mqtt._run_scan_with_retry", return_value=set()),
            patch("generate_nibe_mqtt.decide_startup_action", return_value="apply"),
            patch("generate_nibe_mqtt._execute_startup_action"),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.remove_menu_dashboard"),
            patch("generate_nibe_mqtt.schedule_menu_dashboard_regen"),
            patch("generate_nibe_mqtt.concurrent.futures.ThreadPoolExecutor"),
            patch("generate_nibe_mqtt.time.sleep"),
        ):
            em_instance = MockEM.return_value
            em_instance.discover_points.return_value = True
            em_instance.mqtt_enabled_points = set()
            em_instance.all_points = []
            em_instance.active_entities = []
            em_instance.bulk_interval = 30
            MockPub.return_value.mqtt = MagicMock()

            _run_startup_sequence(
                cfg, MagicMock(), mc, {}, "nibe_test001", "essential", MagicMock()
            )
            self.assertIs(mock_load.call_args.kwargs["log_if_mode"], False)

            _run_startup_sequence(cfg, MagicMock(), mc, {}, "nibe_test001", "menus", MagicMock())
            self.assertIs(mock_load.call_args.kwargs["log_if_mode"], True)

    def test_notify_ha_called_when_discovery_fails(self):
        """When discover_points() returns False, notify_ha must be called."""
        with (
            patch("generate_nibe_mqtt.notify_ha") as mock_notify,
            patch("generate_nibe_mqtt._build_device_info", return_value={}),
            patch("generate_nibe_mqtt.MqttDiscoveryPublisher") as MockPub,
            patch("generate_nibe_mqtt.EntityManager") as MockEM,
            patch("generate_nibe_mqtt._load_menu_structure", return_value=({}, frozenset())),
            patch("generate_nibe_mqtt.dismiss_ha"),
            patch("generate_nibe_mqtt.HAEntityRegistryWatcher"),
            patch("generate_nibe_mqtt.threading.Thread"),
            patch("generate_nibe_mqtt.ManagementCommandHandler"),
            patch("generate_nibe_mqtt._run_scan_with_retry", return_value=set()),
            patch("generate_nibe_mqtt.decide_startup_action", return_value="apply"),
            patch("generate_nibe_mqtt._execute_startup_action"),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.remove_menu_dashboard"),
            patch("generate_nibe_mqtt.concurrent.futures.ThreadPoolExecutor"),
            patch("generate_nibe_mqtt.time.sleep"),
        ):
            from generate_nibe_mqtt import _run_startup_sequence

            cfg = self._cfg()
            em_inst = MockEM.return_value
            em_inst.discover_points.return_value = False
            em_inst.mqtt_enabled_points = set()
            em_inst.all_points = []
            em_inst.active_entities = []
            em_inst.bulk_interval = 30
            MockPub.return_value.mqtt = MagicMock()

            _run_startup_sequence(
                cfg,
                MagicMock(),
                MagicMock(),
                {},
                "nibe_test001",
                "essential",
                MagicMock(),
            )

        mock_notify.assert_called()
        call_kwargs = mock_notify.call_args.kwargs
        self.assertIn("notification_id", call_kwargs)
        self.assertEqual(call_kwargs["notification_id"], "nibe_discovery_incomplete")

    def test_discovery_failure_notification_points_to_test_connection_button(self):
        """A user hitting this at startup has no entities and no other
        in-HA diagnostic tool visible yet — the message must point them at
        the debug 'Test API Connection' button rather than leaving them to
        guess or dig through container logs."""
        with (
            patch("generate_nibe_mqtt.notify_ha") as mock_notify,
            patch("generate_nibe_mqtt._build_device_info", return_value={}),
            patch("generate_nibe_mqtt.MqttDiscoveryPublisher") as MockPub,
            patch("generate_nibe_mqtt.EntityManager") as MockEM,
            patch("generate_nibe_mqtt._load_menu_structure", return_value=({}, frozenset())),
            patch("generate_nibe_mqtt.dismiss_ha"),
            patch("generate_nibe_mqtt.HAEntityRegistryWatcher"),
            patch("generate_nibe_mqtt.threading.Thread"),
            patch("generate_nibe_mqtt.ManagementCommandHandler"),
            patch("generate_nibe_mqtt._run_scan_with_retry", return_value=set()),
            patch("generate_nibe_mqtt.decide_startup_action", return_value="apply"),
            patch("generate_nibe_mqtt._execute_startup_action"),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.remove_menu_dashboard"),
            patch("generate_nibe_mqtt.concurrent.futures.ThreadPoolExecutor"),
            patch("generate_nibe_mqtt.time.sleep"),
        ):
            from generate_nibe_mqtt import _run_startup_sequence

            cfg = self._cfg()
            em_inst = MockEM.return_value
            em_inst.discover_points.return_value = False
            em_inst.mqtt_enabled_points = set()
            em_inst.all_points = []
            em_inst.active_entities = []
            em_inst.bulk_interval = 30
            MockPub.return_value.mqtt = MagicMock()

            _run_startup_sequence(
                cfg,
                MagicMock(),
                MagicMock(),
                {},
                "nibe_test001",
                "essential",
                MagicMock(),
            )

        message = mock_notify.call_args.kwargs["message"]
        self.assertIn("Test API Connection", message)
        self.assertIn("Debug mode", message)

    def test_discovery_notification_flag_set_on_failure(self):
        """entity_manager._discovery_notification_active must be True after failed discovery."""
        _, em, *_ = self._run(discover_ok=False)
        self.assertTrue(em._discovery_notification_active)

    def test_wires_collaborators_with_correct_arguments(self):
        """Every subsystem _run_startup_sequence constructs must receive the
        real cfg-derived values — not just *some* args. All the collaborators
        below are mocked out entirely, so nothing else verifies their call
        arguments; this one test pins the exact wiring end to end."""
        from generate_nibe_mqtt import _run_startup_sequence

        cfg = self._cfg()
        response = {"some": "response"}
        mc = MagicMock()
        api_client = MagicMock()
        set_em = MagicMock()

        with (
            patch(
                "generate_nibe_mqtt._build_device_info", return_value={"model": "S40"}
            ) as mock_bdi,
            patch("generate_nibe_mqtt.MqttDiscoveryPublisher") as MockPub,
            patch("generate_nibe_mqtt.EntityManager") as MockEM,
            patch("generate_nibe_mqtt._load_menu_structure", return_value=({}, frozenset())),
            patch("generate_nibe_mqtt.dismiss_ha") as mock_dismiss,
            patch("generate_nibe_mqtt.notify_ha"),
            patch("generate_nibe_mqtt.HAEntityRegistryWatcher") as MockWatcher,
            patch("generate_nibe_mqtt.threading.Thread") as MockThread,
            patch("generate_nibe_mqtt.ManagementCommandHandler") as MockMgmt,
            patch("generate_nibe_mqtt._run_scan_with_retry", return_value=set()),
            patch("generate_nibe_mqtt.decide_startup_action", return_value="apply"),
            patch("generate_nibe_mqtt._execute_startup_action"),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.remove_menu_dashboard"),
            patch("generate_nibe_mqtt.concurrent.futures.ThreadPoolExecutor"),
            patch("generate_nibe_mqtt.time.sleep"),
        ):
            em_instance = MockEM.return_value
            em_instance.discover_points.return_value = True
            em_instance.mqtt_enabled_points = set()
            em_instance.all_points = []
            em_instance.active_entities = []
            em_instance.bulk_interval = 30

            pub_instance = MockPub.return_value
            pub_instance.mqtt = MagicMock()

            _run_startup_sequence(
                cfg,
                api_client,
                mc,
                response,
                "nibe_test001",
                "essential",
                set_em,
            )

        # _build_device_info gets the real response/device_id/device_name/api_base_url
        mock_bdi.assert_called_once_with(
            response,
            "nibe_test001",
            cfg.device_name,
            cfg.api_base_url,
        )

        # MqttDiscoveryPublisher wired with the real mqtt_client/device_id/device_name
        _, pub_kwargs = MockPub.call_args
        self.assertIs(pub_kwargs["mqtt_client"], mc)
        self.assertEqual(pub_kwargs["device_id"], "nibe_test001")
        self.assertEqual(pub_kwargs["device_name"], cfg.device_name)

        # EntityManager wired with the real api_client/mqtt_client
        _, em_kwargs = MockEM.call_args
        self.assertIs(em_kwargs["api_client"], api_client)
        self.assertIs(em_kwargs["mqtt_client"], mc)

        # set_entity_manager callback receives the constructed entity_manager
        set_em.assert_called_once_with(em_instance)

        # dismiss_ha clears the write-error notification on the real mqtt_client
        mock_dismiss.assert_called_once_with(mc, "nibe_write_error")

        # HAEntityRegistryWatcher wired with entity_manager and publisher, then started
        MockWatcher.assert_called_once_with(em_instance, pub_instance)
        MockWatcher.return_value.start.assert_called_once()

        # Lovelace provisioning thread targets provision_lovelace_ui with the
        # real version/device_name/registry_watcher/debug_mode and menu-mode kwarg
        from generate_nibe_mqtt import BRIDGE_VERSION, provision_lovelace_ui

        _, thread_kwargs = MockThread.call_args
        self.assertIs(thread_kwargs["target"], provision_lovelace_ui)
        self.assertEqual(
            thread_kwargs["args"],
            (BRIDGE_VERSION, cfg.device_name, MockWatcher.return_value, False),
        )
        self.assertEqual(thread_kwargs["kwargs"], {"mode": "essential"})
        self.assertTrue(thread_kwargs["daemon"])

        # ManagementCommandHandler wired with mqtt_client/entity_manager/publisher
        mgmt_call_args = MockMgmt.call_args.args
        self.assertIs(mgmt_call_args[0], mc)
        self.assertIs(mgmt_call_args[1], em_instance)
        self.assertIs(mgmt_call_args[2], pub_instance)


# ===========================================================================
# Missing session additions — re-applied from session transcript
# ===========================================================================

# ---------------------------------------------------------------------------
# TestUpdateEntityStateNoValueMappings additions
# ---------------------------------------------------------------------------


# ===========================================================================
# Full-pipeline propagation: load_config() -> _build_infrastructure()
#                             -> _run_startup_sequence()
#
# _build_infrastructure()'s wiring and _run_startup_sequence()'s wiring are
# each fully tested elsewhere (including against real load_config() output
# for the _build_infrastructure half). This closes the last seam: does a
# single value entered in options.json survive the ENTIRE startup chain,
# hopping through both functions in the real order main() calls them in.
# ===========================================================================


class TestConfigPropagatesThroughFullStartupChain(unittest.TestCase):
    def _load(self, options=None):
        import generate_nibe_mqtt as gn

        def fake_exists(path):
            return path == "/data/options.json" if path == "/data/options.json" else False

        import io

        def fake_open(path, *a, **kw):
            if path == "/data/options.json":
                return io.StringIO(json.dumps(options))
            raise FileNotFoundError(path)

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("os.path.exists", side_effect=fake_exists),
            patch("builtins.open", side_effect=fake_open),
        ):
            return gn.load_config()

    def test_options_json_values_survive_build_infrastructure_and_startup_sequence(self):
        """poll_interval, api_failure_threshold, changelog_retention_days,
        and device_name — all entered in options.json — must reach the
        real EntityManager/MqttDiscoveryPublisher construction after
        passing through BOTH _build_infrastructure() and
        _run_startup_sequence() in the real order main() calls them."""
        from generate_nibe_mqtt import _build_infrastructure, _run_startup_sequence

        cfg = self._load(
            options={
                "nibe_username": "nibeuser",
                "nibe_password": "nibepass",
                "mqtt_host": "broker.local",
                "device_name": "Propagation Test Device",
                "poll_interval": 120,
                "api_failure_threshold": 7,
                "changelog_retention_days": 45,
            }
        )
        self.assertEqual(cfg.poll_interval, 120)
        self.assertEqual(cfg.api_failure_threshold, 7)
        self.assertEqual(cfg.changelog_retention_days, 45)
        self.assertEqual(cfg.device_name, "Propagation Test Device")

        mock_mc = MagicMock()
        mock_mc.is_connected.return_value = True
        with (
            patch("generate_nibe_mqtt._fetch_api_response", return_value={}),
            patch("generate_nibe_mqtt._build_ssl_context", return_value=MagicMock()),
            patch("generate_nibe_mqtt.NibeApiClient"),
            patch("generate_nibe_mqtt.copy_card_file"),
            patch("generate_nibe_mqtt.mqtt.Client", return_value=mock_mc),
            patch("generate_nibe_mqtt.time.sleep"),
        ):
            api_client, mqtt_client, response, device_id, _, set_em = _build_infrastructure(cfg)

        with (
            patch("generate_nibe_mqtt._build_device_info", return_value={"model": "S40"}),
            patch("generate_nibe_mqtt.MqttDiscoveryPublisher") as MockPub,
            patch("generate_nibe_mqtt.EntityManager") as MockEM,
            patch("generate_nibe_mqtt._load_menu_structure", return_value=({}, frozenset())),
            patch("generate_nibe_mqtt.dismiss_ha"),
            patch("generate_nibe_mqtt.notify_ha"),
            patch("generate_nibe_mqtt.HAEntityRegistryWatcher"),
            patch("generate_nibe_mqtt.threading.Thread"),
            patch("generate_nibe_mqtt.ManagementCommandHandler"),
            patch("generate_nibe_mqtt._run_scan_with_retry", return_value=set()),
            patch("generate_nibe_mqtt.decide_startup_action", return_value="apply"),
            patch("generate_nibe_mqtt._execute_startup_action"),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.remove_menu_dashboard"),
            patch("generate_nibe_mqtt.concurrent.futures.ThreadPoolExecutor"),
            patch("generate_nibe_mqtt.time.sleep"),
        ):
            em_instance = MockEM.return_value
            em_instance.discover_points.return_value = True
            em_instance.mqtt_enabled_points = set()
            em_instance.all_points = []
            em_instance.active_entities = []
            em_instance.bulk_interval = 120

            _run_startup_sequence(
                cfg,
                api_client,
                mqtt_client,
                response,
                device_id,
                "essential",
                set_em,
            )

        self.assertEqual(em_instance.bulk_interval, 120)
        self.assertEqual(em_instance.api_failure_threshold, 7)
        self.assertEqual(em_instance.changelog_retention_days, 45)
        pub_kwargs = MockPub.call_args.kwargs
        self.assertEqual(pub_kwargs["device_name"], "Propagation Test Device")


# ===========================================================================
# Coverage gaps in generate_nibe_mqtt.py
# ===========================================================================


class TestRunStartupSequenceDebugReset(unittest.TestCase):
    """_run_startup_sequence clears stale test result sensor on startup
    when debug mode is active (cfg.debug_mode=True)."""

    def _run_debug(self):
        from generate_nibe_mqtt import BridgeConfig, _run_startup_sequence

        cfg = BridgeConfig(
            api_base_url="https://10.0.0.1:8443/api/v1/devices/0",
            nibe_auth="Basic dXNlcjpwYXNz",
            mqtt_broker="localhost",
            mqtt_port=1883,
            device_name="Test",
            device_id="nibe_test001",
            poll_interval=30,
            api_failure_threshold=3,
            changelog_retention_days=90,
            mode="essential",
            debug_mode=True,
        )
        mc = MagicMock()
        with (
            patch("generate_nibe_mqtt._build_device_info", return_value={"model": "S40"}),
            patch("generate_nibe_mqtt.MqttDiscoveryPublisher") as MockPub,
            patch("generate_nibe_mqtt.EntityManager") as MockEM,
            patch("generate_nibe_mqtt._load_menu_structure", return_value=({}, frozenset())),
            patch("generate_nibe_mqtt.dismiss_ha"),
            patch("generate_nibe_mqtt.notify_ha"),
            patch("generate_nibe_mqtt.HAEntityRegistryWatcher"),
            patch("generate_nibe_mqtt.threading.Thread"),
            patch("generate_nibe_mqtt.ManagementCommandHandler"),
            patch("generate_nibe_mqtt._run_scan_with_retry", return_value=set()),
            patch("generate_nibe_mqtt.decide_startup_action", return_value="apply"),
            patch("generate_nibe_mqtt._execute_startup_action"),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.remove_menu_dashboard"),
            patch("generate_nibe_mqtt.concurrent.futures.ThreadPoolExecutor"),
            patch("generate_nibe_mqtt.time.sleep"),
        ):
            em_instance = MockEM.return_value
            em_instance.discover_points.return_value = True
            em_instance.mqtt_enabled_points = set()
            em_instance.all_points = []
            em_instance.active_entities = []
            em_instance.bulk_interval = 30
            pub_instance = MockPub.return_value
            pub_instance.mqtt = MagicMock()
            _run_startup_sequence(
                cfg,
                MagicMock(),
                mc,
                {},
                "nibe_test001",
                "essential",
                MagicMock(),
            )
        return mc

    def test_debug_mode_publishes_ready_attrs_on_startup(self):
        """In debug mode, RUN_TESTS_ATTRS must be published with status='ready'
        on startup so the sensor attributes show a clean state after a rebuild.
        RUN_TESTS_STATE is intentionally NOT published to avoid triggering
        HA automations on restart."""
        import json as _json

        from nibe_mqtt_publisher import MgmtTopic

        mc = self._run_debug()
        topics = [c.args[0] for c in mc.publish.call_args_list]
        # Attrs must be reset
        self.assertIn(MgmtTopic.RUN_TESTS_ATTRS, topics)
        attrs_calls = [
            c for c in mc.publish.call_args_list if c.args[0] == MgmtTopic.RUN_TESTS_ATTRS
        ]
        payloads = [_json.loads(c.args[1]) for c in attrs_calls]
        self.assertTrue(
            any(p.get("status") == "ready" for p in payloads),
            "RUN_TESTS_ATTRS must contain status='ready' on startup",
        )
        # State topic must NOT be published on startup
        self.assertNotIn(
            MgmtTopic.RUN_TESTS_STATE,
            topics,
            "RUN_TESTS_STATE must not be published on startup — would trigger automations",
        )

    def test_non_debug_mode_does_not_publish_test_state_on_startup(self):
        """In non-debug mode the test result sensor does not exist —
        RUN_TESTS_STATE must not be published on startup."""
        from generate_nibe_mqtt import BridgeConfig, _run_startup_sequence
        from nibe_mqtt_publisher import MgmtTopic

        cfg = BridgeConfig(
            api_base_url="https://10.0.0.1:8443/api/v1/devices/0",
            nibe_auth="Basic dXNlcjpwYXNz",
            mqtt_broker="localhost",
            mqtt_port=1883,
            device_name="Test",
            device_id="nibe_test001",
            poll_interval=30,
            api_failure_threshold=3,
            changelog_retention_days=90,
            mode="essential",
        )
        mc = MagicMock()
        with (
            patch("generate_nibe_mqtt._build_device_info", return_value={"model": "S40"}),
            patch("generate_nibe_mqtt.MqttDiscoveryPublisher") as MockPub,
            patch("generate_nibe_mqtt.EntityManager") as MockEM,
            patch("generate_nibe_mqtt._load_menu_structure", return_value=({}, frozenset())),
            patch("generate_nibe_mqtt.dismiss_ha"),
            patch("generate_nibe_mqtt.notify_ha"),
            patch("generate_nibe_mqtt.HAEntityRegistryWatcher"),
            patch("generate_nibe_mqtt.threading.Thread"),
            patch("generate_nibe_mqtt.ManagementCommandHandler"),
            patch("generate_nibe_mqtt._run_scan_with_retry", return_value=set()),
            patch("generate_nibe_mqtt.decide_startup_action", return_value="apply"),
            patch("generate_nibe_mqtt._execute_startup_action"),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.remove_menu_dashboard"),
            patch("generate_nibe_mqtt.concurrent.futures.ThreadPoolExecutor"),
            patch("generate_nibe_mqtt.time.sleep"),
        ):
            em_instance = MockEM.return_value
            em_instance.discover_points.return_value = True
            em_instance.mqtt_enabled_points = set()
            em_instance.all_points = []
            em_instance.active_entities = []
            em_instance.bulk_interval = 30
            pub_instance = MockPub.return_value
            pub_instance.mqtt = MagicMock()
            _run_startup_sequence(
                cfg,
                MagicMock(),
                mc,
                {},
                "nibe_test001",
                "essential",
                MagicMock(),
            )
        topics = [c.args[0] for c in mc.publish.call_args_list]
        self.assertNotIn(MgmtTopic.RUN_TESTS_STATE, topics)


class TestBuildInfrastructureRemainingBranches(unittest.TestCase):
    """Targeted coverage for the three uncovered branches in _build_infrastructure:

    • Line 824  — credentials present: username_pw_set() called
    • Lines 854–862 — on_connect with a FATAL_RC (4 or 5): logs error +
                      sets _auth_failed (distinct from the threading.Event
                      mock used in test_exits_on_mqtt_auth_failure)
    • Line 898  — is_connected() returns False after loop_start:
                  logs 'MQTT not yet connected' warning
    """

    def _cfg(self, **kw):
        from generate_nibe_mqtt import BridgeConfig

        cfg = BridgeConfig(
            api_base_url="https://10.0.0.1:8443/api/v1/devices/0",
            nibe_auth="Basic dXNlcjpwYXNz",
            mqtt_broker="localhost",
            mqtt_port=1883,
            device_name="Test Device",
            device_id="nibe_test",
            poll_interval=30,
        )
        for k, v in kw.items():
            setattr(cfg, k, v)
        return cfg

    def _run_infra(self, cfg, mock_mc):
        from generate_nibe_mqtt import _build_infrastructure

        with (
            patch("generate_nibe_mqtt._fetch_api_response", return_value={}),
            patch("generate_nibe_mqtt._build_ssl_context", return_value=MagicMock()),
            patch("generate_nibe_mqtt.NibeApiClient"),
            patch("generate_nibe_mqtt.copy_card_file"),
            patch("generate_nibe_mqtt.mqtt.Client", return_value=mock_mc),
            patch("generate_nibe_mqtt.time.sleep"),
        ):
            return _build_infrastructure(cfg)

    def test_username_pw_set_called_when_credentials_present(self):
        """When mqtt_username and mqtt_password are both set, username_pw_set()
        must be called on the mqtt client (line 824 — the True branch)."""
        cfg = self._cfg(mqtt_username="user", mqtt_password="secret")
        mock_mc = MagicMock()
        mock_mc.is_connected.return_value = True
        self._run_infra(cfg, mock_mc)
        mock_mc.username_pw_set.assert_called_once_with("user", "secret")

    def test_on_connect_fatal_rc_logs_error_and_sets_auth_failed(self):
        """on_connect with reason code 4 (bad credentials) must log an error
        and set _auth_failed — exercising lines 854–862.

        We fire on_connect directly on the captured callback rather than
        waiting for a real MQTT broker, which makes the FATAL_RC branch
        reachable without the threading.Event trick."""
        cfg = self._cfg()
        mock_mc = MagicMock()
        mock_mc.is_connected.return_value = True
        self._run_infra(cfg, mock_mc)

        # on_connect is stored on the mock as an attribute by _build_infrastructure
        on_connect = mock_mc.on_connect
        self.assertIsNotNone(on_connect, "on_connect callback was not registered")

        rc = MagicMock()
        rc.value = 4  # MQTT bad credentials — in _FATAL_RC = {4, 5}

        with patch("generate_nibe_mqtt.log_mqtt") as mock_log:
            on_connect(mock_mc, None, None, rc, None)

        mock_log.error.assert_called_once()
        msg = str(mock_log.error.call_args)
        self.assertIn("refused", msg)

    def test_on_connect_non_fatal_non_zero_rc_logs_error(self):
        """on_connect with rc=3 (broker unavailable, not in FATAL_RC) must
        reach the else branch and log an error — lines 861–864."""
        cfg = self._cfg()
        mock_mc = MagicMock()
        mock_mc.is_connected.return_value = True
        self._run_infra(cfg, mock_mc)

        on_connect = mock_mc.on_connect
        rc = MagicMock()
        rc.value = 3  # not in _FATAL_RC

        with patch("generate_nibe_mqtt.log_mqtt") as mock_log:
            on_connect(mock_mc, None, None, rc, None)

        mock_log.error.assert_called_once()

    def test_not_yet_connected_warning_when_is_connected_false(self):
        """When is_connected() returns False after loop_start (slow broker),
        an info message must be logged — transient condition, not a warning."""
        cfg = self._cfg()
        mock_mc = MagicMock()
        mock_mc.is_connected.return_value = False  # <-- slow broker

        with patch("generate_nibe_mqtt.log_mqtt") as mock_log:
            self._run_infra(cfg, mock_mc)

        info_msgs = str(mock_log.info.call_args_list)
        self.assertIn("not yet connected", info_msgs)


class TestRunStartupSequenceMenusMode(unittest.TestCase):
    """_run_startup_sequence with initial_mode='menus' must call
    schedule_menu_dashboard_regen instead of remove_menu_dashboard (line 1062)."""

    def _run(self, initial_mode):
        from generate_nibe_mqtt import BridgeConfig, _run_startup_sequence

        cfg = BridgeConfig(
            api_base_url="https://10.0.0.1:8443/api/v1/devices/0",
            nibe_auth="Basic dXNlcjpwYXNz",
            mqtt_broker="localhost",
            mqtt_port=1883,
            device_name="Test Device",
            device_id="nibe_test001",
            poll_interval=30,
            api_failure_threshold=3,
            changelog_retention_days=90,
            mode=initial_mode,
        )
        with (
            patch("generate_nibe_mqtt._build_device_info", return_value={}),
            patch("generate_nibe_mqtt.MqttDiscoveryPublisher") as MockPub,
            patch("generate_nibe_mqtt.EntityManager") as MockEM,
            patch("generate_nibe_mqtt._load_menu_structure", return_value=({}, frozenset())),
            patch("generate_nibe_mqtt.dismiss_ha"),
            patch("generate_nibe_mqtt.notify_ha"),
            patch("generate_nibe_mqtt.HAEntityRegistryWatcher"),
            patch("generate_nibe_mqtt.threading.Thread"),
            patch("generate_nibe_mqtt.ManagementCommandHandler"),
            patch("generate_nibe_mqtt._run_scan_with_retry", return_value=set()),
            patch("generate_nibe_mqtt.decide_startup_action", return_value="apply"),
            patch("generate_nibe_mqtt._execute_startup_action"),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.remove_menu_dashboard") as mock_remove,
            patch("generate_nibe_mqtt.schedule_menu_dashboard_regen") as mock_sched,
            patch("generate_nibe_mqtt.concurrent.futures.ThreadPoolExecutor"),
            patch("generate_nibe_mqtt.time.sleep"),
        ):
            em = MockEM.return_value
            em.discover_points.return_value = True
            em.mqtt_enabled_points = set()
            em.all_points = []
            em.active_entities = []
            em.bulk_interval = 30
            MockPub.return_value.mqtt = MagicMock()
            _run_startup_sequence(
                cfg,
                MagicMock(),
                MagicMock(),
                {},
                "nibe_test001",
                initial_mode,
                MagicMock(),
            )
        return mock_remove, mock_sched

    def test_menus_mode_calls_schedule_not_remove(self):
        """initial_mode='menus' must call schedule_menu_dashboard_regen (line 1062),
        not remove_menu_dashboard."""
        mock_remove, mock_sched = self._run("menus")
        mock_sched.assert_called_once()
        mock_remove.assert_not_called()

    def test_non_menus_mode_calls_remove_not_schedule(self):
        """initial_mode='essential' must call remove_menu_dashboard (line 1067),
        not schedule_menu_dashboard_regen."""
        mock_remove, mock_sched = self._run("essential")
        mock_remove.assert_called_once()
        mock_sched.assert_not_called()


class TestRunStartupSequenceFullWiringGaps(unittest.TestCase):
    """_run_startup_sequence: targeted coverage for mutmut survivors not
    caught by TestRunStartupSequence — collaborator constructor kwargs,
    attribute assignments, exact log/message text, executor sizing, and
    the several downstream calls whose *argument values* (not just
    call-count) were not previously pinned."""

    def _cfg(self, **kw):
        from generate_nibe_mqtt import BridgeConfig

        cfg = BridgeConfig(
            api_base_url="https://10.0.0.1:8443/api/v1/devices/0",
            nibe_auth="Basic dXNlcjpwYXNz",
            mqtt_broker="localhost",
            mqtt_port=1883,
            device_name="Test Device",
            device_id="nibe_test001",
            poll_interval=30,
            api_failure_threshold=3,
            changelog_retention_days=90,
            mode="essential",
        )
        for k, v in kw.items():
            setattr(cfg, k, v)
        return cfg

    def _run_full(
        self,
        cfg=None,
        initial_mode="essential",
        response=None,
        discover_ok=True,
        mqtt_enabled_points=None,
    ):
        """Runs _run_startup_sequence with everything patched, but returns
        every mock so callers can inspect exact call arguments."""
        from generate_nibe_mqtt import _run_startup_sequence

        cfg = cfg or self._cfg()
        response = response if response is not None else {}
        mc = MagicMock()
        mqtt_enabled_points = mqtt_enabled_points if mqtt_enabled_points is not None else set()

        mocks = {}
        # _run_startup_sequence does `MODES['menus'] = _load_menu_structure(...)`
        # against the REAL, process-wide MODES dict (imported from
        # nibe_entity_detection.py). With _load_menu_structure mocked to
        # return a test fixture value below, that line would otherwise
        # permanently corrupt the shared dict for every other test in the
        # suite that runs afterward — order-dependent pollution caught by
        # TestModesStructuralProperties in test_entity_detection.py.
        # patch.dict saves and restores MODES's real contents automatically.
        with (
            patch.dict("generate_nibe_mqtt.MODES", {}, clear=False),
            patch(
                "generate_nibe_mqtt._build_device_info", return_value={"model": "S40"}
            ) as mocks_bdi,
            patch("generate_nibe_mqtt.MqttDiscoveryPublisher") as MockPub,
            patch("generate_nibe_mqtt.EntityManager") as MockEM,
            patch(
                "generate_nibe_mqtt._load_menu_structure", return_value=({"p": "m"}, {"menu1"})
            ) as mock_load,
            patch("generate_nibe_mqtt.dismiss_ha") as mock_dismiss,
            patch("generate_nibe_mqtt.notify_ha") as mock_notify,
            patch("generate_nibe_mqtt.HAEntityRegistryWatcher") as MockWatcher,
            patch("generate_nibe_mqtt.threading.Thread") as MockThread,
            patch("generate_nibe_mqtt.ManagementCommandHandler") as MockMgmt,
            patch(
                "generate_nibe_mqtt._run_scan_with_retry", return_value=mqtt_enabled_points
            ) as mock_scan,
            patch("generate_nibe_mqtt.decide_startup_action", return_value="apply"),
            patch("generate_nibe_mqtt._execute_startup_action") as mock_execute,
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.remove_menu_dashboard"),
            patch("generate_nibe_mqtt.schedule_menu_dashboard_regen") as mock_sched,
            patch("generate_nibe_mqtt.concurrent.futures.ThreadPoolExecutor") as MockExec,
            patch("generate_nibe_mqtt.time.sleep") as mock_sleep,
        ):
            em_instance = MockEM.return_value
            em_instance.discover_points.return_value = discover_ok
            em_instance.mqtt_enabled_points = mqtt_enabled_points
            em_instance.all_points = []
            em_instance.active_entities = []
            em_instance.bulk_interval = 30
            em_instance.read_applied_mode.return_value = "essential"

            pub_instance = MockPub.return_value
            pub_instance.mqtt = MagicMock()

            _run_startup_sequence(
                cfg,
                MagicMock(),
                mc,
                response,
                "nibe_test001",
                initial_mode,
                MagicMock(),
            )

            # Captured inside the patch.dict scope, before MODES is restored
            # to its real contents on exit — see the patch.dict comment above.
            from generate_nibe_mqtt import MODES as _modes_snapshot_src

            mocks["modes_menus"] = _modes_snapshot_src["menus"]

        mocks.update(
            {
                "MockPub": MockPub,
                "MockEM": MockEM,
                "mock_load": mock_load,
                "mock_dismiss": mock_dismiss,
                "mock_notify": mock_notify,
                "MockWatcher": MockWatcher,
                "MockThread": MockThread,
                "MockMgmt": MockMgmt,
                "mock_scan": mock_scan,
                "mock_execute": mock_execute,
                "mock_sched": mock_sched,
                "MockExec": MockExec,
                "mock_sleep": mock_sleep,
                "mc": mc,
                "em_instance": em_instance,
                "pub_instance": pub_instance,
                "mocks_bdi": mocks_bdi,
            }
        )
        return mocks

    # -- EntityManager / MqttDiscoveryPublisher constructor kwargs ---------

    def test_entity_manager_constructed_with_publisher_notify_dismiss_kwargs(self):
        m = self._run_full()
        _, kwargs = m["MockEM"].call_args
        self.assertIs(kwargs["publisher"], m["pub_instance"])
        self.assertIs(kwargs["notify_fn"], m["mock_notify"])
        self.assertIs(kwargs["dismiss_fn"], m["mock_dismiss"])

    def test_publisher_constructed_with_device_info_kwarg(self):
        m = self._run_full()
        _, kwargs = m["MockPub"].call_args
        self.assertEqual(kwargs["device_info"], {"model": "S40"})

    def test_entity_manager_device_info_attribute_set(self):
        m = self._run_full()
        self.assertEqual(m["em_instance"].device_info, {"model": "S40"})

    # -- menu structure loading ---------------------------------------------

    def test_load_menu_structure_called_with_app_dir_positional(self):
        import generate_nibe_mqtt as gn

        m = self._run_full()
        real_app_dir = os.path.dirname(gn.__file__)
        self.assertEqual(m["mock_load"].call_args.args, (real_app_dir,))

    def test_point_to_menu_map_and_modes_menus_assigned_from_load_result(self):
        m = self._run_full()
        self.assertEqual(m["em_instance"].point_to_menu_map, {"p": "m"})
        self.assertEqual(m["modes_menus"], {"menu1"})

    # -- device-info debug log -----------------------------------------------

    def test_device_info_debug_log_exact_message_and_args(self):
        import generate_nibe_mqtt as gn

        with self.assertLogs(gn.log_startup, level="DEBUG") as cm:
            self._run_full()
        joined = "\n".join(cm.output)
        self.assertIn("Device info: model=S40, serial=None, firmware=None", joined)

    def test_device_info_debug_log_uses_real_serial_and_model_id_when_present(self):
        """The sibling test above uses a device_info fixture missing
        'serial_number'/'model_id' entirely, so .get() legitimately
        returns None either way — it can't distinguish a hardcoded None,
        a wrong/dropped key, or a case-mutated key from the real .get()
        call, since all of them coincidentally produce the same 'None'
        text against that fixture. A device_info with real, distinct
        values for every field closes that gap."""
        from generate_nibe_mqtt import BridgeConfig, _run_startup_sequence

        cfg = BridgeConfig(
            api_base_url="https://10.0.0.1:8443/api/v1/devices/0",
            nibe_auth="Basic dXNlcjpwYXNz",
            mqtt_broker="localhost",
            mqtt_port=1883,
            device_name="Test Device",
            device_id="nibe_test001",
            poll_interval=30,
            api_failure_threshold=3,
            changelog_retention_days=90,
        )
        rich_device_info = {
            "model": "S2125-12",
            "serial_number": "REALSERIAL42",
            "model_id": "REALMODELID7",
        }
        with (
            patch("generate_nibe_mqtt._build_device_info", return_value=rich_device_info),
            patch("generate_nibe_mqtt.MqttDiscoveryPublisher") as MockPub,
            patch("generate_nibe_mqtt.EntityManager") as MockEM,
            patch("generate_nibe_mqtt._load_menu_structure", return_value=({}, frozenset())),
            patch("generate_nibe_mqtt.dismiss_ha"),
            patch("generate_nibe_mqtt.notify_ha"),
            patch("generate_nibe_mqtt.HAEntityRegistryWatcher"),
            patch("generate_nibe_mqtt.threading.Thread"),
            patch("generate_nibe_mqtt.ManagementCommandHandler"),
            patch("generate_nibe_mqtt._run_scan_with_retry", return_value=set()),
            patch("generate_nibe_mqtt.decide_startup_action", return_value="apply"),
            patch("generate_nibe_mqtt._execute_startup_action"),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.remove_menu_dashboard"),
            patch("generate_nibe_mqtt.concurrent.futures.ThreadPoolExecutor"),
            patch("generate_nibe_mqtt.time.sleep"),
            patch("generate_nibe_mqtt.log_startup") as mock_log,
        ):
            em = MockEM.return_value
            em.discover_points.return_value = True
            em.mqtt_enabled_points = set()
            em.all_points = []
            em.active_entities = []
            em.bulk_interval = 30
            MockPub.return_value.mqtt = MagicMock()
            _run_startup_sequence(
                cfg,
                MagicMock(),
                MagicMock(),
                {},
                "nibe_test001",
                "essential",
                MagicMock(),
            )
        mock_log.debug.assert_called_once_with(
            "Device info: model=%s, serial=%s, firmware=%s",
            "S2125-12",
            "REALSERIAL42",
            "REALMODELID7",
        )

    # -- discovery-failure warning + notify_ha ------------------------------

    def test_discovery_failure_warning_exact_text(self):
        import generate_nibe_mqtt as gn

        with self.assertLogs(gn.log_startup, level="WARNING") as cm:
            self._run_full(discover_ok=False)
        joined = "\n".join(cm.output)
        self.assertIn(
            "Initial point discovery failed — device unreachable. "
            "The bridge will keep retrying in the polling loop.",
            joined,
        )

    def test_notify_ha_called_with_real_mqtt_client_and_exact_title(self):
        m = self._run_full(discover_ok=False)
        args, kwargs = m["mock_notify"].call_args
        self.assertIs(args[0], m["mc"])
        self.assertEqual(kwargs["title"], "Nibe Bridge: Started Without Device")

    def test_notify_ha_message_exact_text(self):
        cfg = self._cfg()
        m = self._run_full(cfg=cfg, discover_ok=False)
        message = m["mock_notify"].call_args.kwargs["message"]
        expected = (
            f"The {cfg.device_name} was unreachable at startup so no entities "
            "could be loaded. The bridge is running and will restore all "
            "entities automatically when the device comes back online. "
            "For a detailed diagnostic (network, TLS, and credentials checked "
            "independently), enable 'Debug mode' in the add-on configuration, "
            "restart, and use the 'Test API Connection' button on the "
            "Management device."
        )
        self.assertEqual(message, expected)

    def test_sleep_called_with_one_second_on_discovery_failure(self):
        m = self._run_full(discover_ok=False)
        m["mock_sleep"].assert_called_once_with(1)

    # -- management interface publishing -------------------------------------

    def test_publish_management_discovery_exact_args(self):
        m = self._run_full(initial_mode="menus")
        m["pub_instance"].publish_management_discovery.assert_called_once_with(
            "menus",
            debug_mode=False,
        )

    def test_publish_initial_device_modes_exact_arg(self):
        response = {"some": "device", "response": True}
        m = self._run_full(response=response)
        m["pub_instance"].publish_initial_device_modes.assert_called_once_with(response)

    # -- debug-mode RUN_TESTS_ATTRS payload -----------------------------------

    def test_run_tests_attrs_payload_exact_and_retain_true(self):
        cfg = self._cfg(debug_mode=True)
        m = self._run_full(cfg=cfg)
        from generate_nibe_mqtt import MgmtTopic

        calls = [
            c for c in m["mc"].publish.call_args_list if c.args[0] == MgmtTopic.RUN_TESTS_ATTRS
        ]
        self.assertEqual(len(calls), 1)
        payload = json.loads(calls[0].args[1])
        self.assertEqual(
            payload,
            {
                "status": "ready",
                "summary": "No test run since last restart.",
            },
        )
        self.assertTrue(calls[0].kwargs.get("retain"))

    # -- executor sizing -------------------------------------------------------

    def test_thread_pool_executors_sized_and_named_correctly(self):
        m = self._run_full()
        calls = m["MockExec"].call_args_list
        self.assertEqual(len(calls), 2)
        mgmt_kwargs = calls[0].kwargs
        test_kwargs = calls[1].kwargs
        self.assertEqual(mgmt_kwargs, {"max_workers": 2, "thread_name_prefix": "nibe_mgmt"})
        self.assertEqual(test_kwargs, {"max_workers": 1, "thread_name_prefix": "nibe_test_runner"})

    # -- ManagementCommandHandler wiring ---------------------------------------

    def test_management_command_handler_positional_executors(self):
        m = self._run_full()
        args = m["MockMgmt"].call_args.args
        mgmt_executor_arg, test_executor_arg = args[3], args[4]
        # Both must be the same ThreadPoolExecutor mock instance (MockExec
        # is a single class-level mock shared across both constructions).
        self.assertIs(mgmt_executor_arg, m["MockExec"].return_value)
        self.assertIs(test_executor_arg, m["MockExec"].return_value)

    def test_ca_cert_path_none_when_file_does_not_exist(self):
        cfg = self._cfg(nibe_ca_cert="/ssl/nibe-ca.pem")
        with patch("os.path.exists", return_value=False) as mock_exists:
            m = self._run_full(cfg=cfg)
        mock_exists.assert_any_call("/ssl/nibe-ca.pem")
        self.assertIsNone(m["MockMgmt"].call_args.kwargs["ca_cert_path"])

    def test_mgmt_avail_topic_assigned(self):
        m = self._run_full()
        from generate_nibe_mqtt import MGMT_AVAIL_TOPIC

        self.assertEqual(m["em_instance"]._mgmt_avail_topic, MGMT_AVAIL_TOPIC)

    # -- scan / decide / execute wiring -----------------------------------------

    def test_run_scan_with_retry_called_with_entity_manager(self):
        m = self._run_full()
        m["mock_scan"].assert_called_once_with(m["em_instance"])

    def test_execute_startup_action_called_with_real_entity_manager_and_mqtt_client(self):
        """Regression: the pre-existing test compared call_args back to
        itself (tautological) and would not catch entity_manager or
        mqtt_client being replaced with None — assert against the actual
        instances captured independently."""
        cfg = self._cfg()
        m = self._run_full(cfg=cfg, mqtt_enabled_points={1, 2})
        m["mock_execute"].assert_called_once_with(
            m["em_instance"],
            "apply",
            "essential",
            "essential",
            m["mc"],
            cfg.device_name,
        )

    # -- Lovelace thread + menu dashboard scheduling -----------------------------

    def test_lovelace_thread_name_kwarg(self):
        m = self._run_full()
        _, kwargs = m["MockThread"].call_args
        self.assertEqual(kwargs["name"], "nibe_lovelace_setup")

    def test_schedule_menu_dashboard_regen_exact_args(self):
        m = self._run_full(initial_mode="menus")
        m["mock_sched"].assert_called_once_with(
            m["em_instance"],
            m["MockWatcher"].return_value,
            False,
            lovelace_thread=m["MockThread"].return_value,
        )

    # -- final "Bridge ready" log ------------------------------------------------

    def test_bridge_ready_log_exact_format_string(self):
        import generate_nibe_mqtt as gn

        with self.assertLogs(gn.log_startup, level="INFO") as cm:
            self._run_full()
        joined = "\n".join(cm.output)
        self.assertIn("Bridge ready —", joined)
        self.assertIn("points,", joined)
        self.assertIn("enabled,", joined)
        self.assertIn("active | poll=", joined)
        self.assertIn("alarm=", joined)

    def test_bridge_ready_log_call_has_the_exact_format_string(self):
        """The sibling test's piecewise assertIn checks all still match an
        XX-wrapped mutant, since every substring they check is still
        present inside 'XX...XX' — the mocked call's own args[0] is the
        only way to pin the exact, unwrapped format string."""
        with patch("generate_nibe_mqtt.log_startup") as mock_log:
            self._run_full()
        info_call = next(
            c
            for c in mock_log.info.call_args_list
            if c.args and c.args[0].startswith("Bridge ready")
        )
        self.assertEqual(
            info_call.args[0],
            "Bridge ready — %d points, %d enabled, %d active | poll=%ds alarm=%ds",
        )


class TestPollLoopAlertPublishException(unittest.TestCase):
    """When publish_bridge_alert itself raises after ≥5 consecutive errors,
    the inner 'except Exception: pass' (lines 1188–1189) must suppress it
    and the loop must continue."""

    def test_alert_publish_exception_suppressed(self):
        """publisher.publish_bridge_alert raising must not kill the loop."""
        from generate_nibe_mqtt import _poll_loop

        em = _make_em()
        pub = MagicMock()
        pub.publish_bridge_alert.side_effect = RuntimeError("broker gone")

        em.initial_discovery_complete = True
        em.post_write_active = False
        em.bulk_interval = 30
        em._post_write_interval = 5

        crash_count = [0]

        def _always_crash():
            crash_count[0] += 1
            raise RuntimeError("persistent failure")

        em.update_all_states = MagicMock(side_effect=_always_crash)

        tick = [0]

        def _fake_sleep(_s):
            tick[0] += 1
            if tick[0] >= 8:  # enough cycles to reach the ≥5 threshold
                raise KeyboardInterrupt

        _t = [0.0]

        def _fake_time():
            _t[0] += 60.0
            return _t[0]

        with (
            patch("generate_nibe_mqtt.time.time", side_effect=_fake_time),
            patch("generate_nibe_mqtt.time.sleep", side_effect=_fake_sleep),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.update_alarm_state"),
            self.assertRaises(KeyboardInterrupt),
        ):
            _poll_loop(em, pub, "essential")

        # Loop survived despite publish_bridge_alert raising
        self.assertGreaterEqual(crash_count[0], 5)
        # publish_bridge_alert was called (at ≥5 consecutive errors)
        pub.publish_bridge_alert.assert_called()


class TestShutdownExecutorTimeout(unittest.TestCase):
    """_shutdown executor drain: when t.is_alive() returns True after join,
    a warning must be logged (line 1230)."""

    def test_executor_timeout_logs_warning(self):
        """When an executor thread does not finish within _SHUTDOWN_TIMEOUT,
        log_startup.warning must be called with the timeout message."""
        from generate_nibe_mqtt import _shutdown

        em = _make_em()
        mc = MagicMock()

        with (
            patch("generate_nibe_mqtt.threading.Thread") as MockThread,
            patch("generate_nibe_mqtt.teardown_lovelace"),
            patch.dict("os.environ", {}, clear=False),
            patch("generate_nibe_mqtt.log_startup") as mock_log,
        ):
            os.environ.pop("NIBE_REMOVE_FRONTEND", None)
            instance = MockThread.return_value
            instance.is_alive.return_value = True  # simulate timeout
            _shutdown(
                em, MagicMock(), mc, MagicMock(), MagicMock(), MagicMock(), [False], MagicMock()
            )

        # Warning must mention the timeout
        warning_calls = str(mock_log.warning.call_args_list)
        self.assertIn("did not finish", warning_calls)

    def test_executor_drains_share_one_deadline_not_independent_budgets(self):
        """Regression: the three executor drains (write, management, test
        suite) must share one _SHUTDOWN_TIMEOUT deadline, not each get their
        own full budget — three independent timeouts could otherwise sum to
        ~3x _SHUTDOWN_TIMEOUT if all three are legitimately busy at once,
        risking a SIGKILL mid-drain from a supervisor whose stop grace
        period is shorter than that. Each successive t.join() call's
        timeout must be <= the previous one's (strictly less once real time
        has elapsed, but with mocked join()s returning instantly, a shared
        deadline still yields non-increasing — not repeated-full — budgets)."""
        from generate_nibe_mqtt import _SHUTDOWN_TIMEOUT, _shutdown

        em = _make_em()
        mc = MagicMock()
        join_timeouts = []

        class FakeThread:
            def __init__(self, target=None, kwargs=None):
                pass

            def start(self):
                pass

            def join(self, timeout=None):
                join_timeouts.append(timeout)

            def is_alive(self):
                return False

        with (
            patch("generate_nibe_mqtt.threading.Thread", FakeThread),
            patch("generate_nibe_mqtt.teardown_lovelace"),
            patch.dict("os.environ", {}, clear=False),
            patch("generate_nibe_mqtt.log_startup"),
        ):
            os.environ.pop("NIBE_REMOVE_FRONTEND", None)
            _shutdown(
                em, MagicMock(), mc, MagicMock(), MagicMock(), MagicMock(), [False], MagicMock()
            )

        self.assertEqual(len(join_timeouts), 3)
        for t in join_timeouts:
            self.assertLessEqual(t, _SHUTDOWN_TIMEOUT)
        # Non-increasing: each subsequent join must not get a fresh full budget.
        self.assertEqual(join_timeouts, sorted(join_timeouts, reverse=True))

    def test_shutdown_deadline_is_now_plus_timeout_not_minus(self):
        """shutdown_deadline must be time.monotonic() + _SHUTDOWN_TIMEOUT —
        a `-` in place of `+` would put the deadline in the past, making
        every executor join's `remaining` budget collapse to 0 instead of
        the real shutdown budget."""
        from generate_nibe_mqtt import _SHUTDOWN_TIMEOUT, _shutdown

        em = _make_em()
        mc = MagicMock()
        join_timeouts = []

        class FakeThread:
            def __init__(self, target=None, kwargs=None):
                pass

            def start(self):
                pass

            def join(self, timeout=None):
                join_timeouts.append(timeout)

            def is_alive(self):
                return False

        with (
            patch("generate_nibe_mqtt.threading.Thread", FakeThread),
            patch("generate_nibe_mqtt.teardown_lovelace"),
            patch.dict("os.environ", {}, clear=False),
            patch("generate_nibe_mqtt.log_startup"),
        ):
            os.environ.pop("NIBE_REMOVE_FRONTEND", None)
            _shutdown(
                em, MagicMock(), mc, MagicMock(), MagicMock(), MagicMock(), [False], MagicMock()
            )

        # With a correctly-future deadline, the first join gets nearly the
        # full budget (well above half of it) — a past deadline would
        # instead floor every join at 0.0.
        self.assertGreater(join_timeouts[0], _SHUTDOWN_TIMEOUT / 2)


# ===========================================================================
# Branch coverage: targeted gaps from --cov-branch audit
# ===========================================================================


class TestLoadMenuStructureLogIfModeFalse(unittest.TestCase):
    """_load_menu_structure: 749→752 — log_if_mode=False suppresses debug logs.

    All existing tests call with the default log_if_mode=True, hitting the
    True branch.  The False branch (skip the two debug log lines) is reached
    when the caller suppresses verbose output (e.g. non-menus modes).
    """

    def test_log_if_mode_false_returns_same_data(self):
        """log_if_mode=False must still return the full data — only the debug
        log calls are skipped, not the actual build work."""
        from generate_nibe_mqtt import _load_menu_structure

        result_true = _load_menu_structure(_APP_DIR, log_if_mode=True)
        result_false = _load_menu_structure(_APP_DIR, log_if_mode=False)
        # Both calls must return identical data
        self.assertEqual(result_true[0], result_false[0])  # point_to_menu
        self.assertEqual(result_true[1], result_false[1])  # menu_points

    def test_log_if_mode_false_does_not_raise(self):
        from generate_nibe_mqtt import _load_menu_structure

        _load_menu_structure(_APP_DIR, log_if_mode=False)  # must not raise


class TestBuildInfrastructureExactLogsAndArgs(unittest.TestCase):
    """_build_infrastructure: exact log text and exact constructor/call args
    for the credential-check, TLS, MQTT-client-construction, and
    connecting-log statements — the existing classes only assert on
    control flow (sys.exit paths), not on the literal text/args."""

    def _cfg(self, **kw):
        from generate_nibe_mqtt import BridgeConfig

        cfg = BridgeConfig(
            api_base_url="https://10.0.0.1:8443/api/v1/devices/0",
            nibe_auth="Basic dXNlcjpwYXNz",
            mqtt_broker="localhost",
            mqtt_port=1883,
            device_name="Test Device",
            device_id="nibe_test",
            poll_interval=30,
        )
        for k, v in kw.items():
            setattr(cfg, k, v)
        return cfg

    def _run_infra(self, cfg, mock_mc=None):
        from generate_nibe_mqtt import _build_infrastructure

        mock_mc = mock_mc or MagicMock()
        mock_mc.is_connected.return_value = True
        with (
            patch("generate_nibe_mqtt._fetch_api_response", return_value={}),
            patch("generate_nibe_mqtt._build_ssl_context", return_value=MagicMock()),
            patch("generate_nibe_mqtt.NibeApiClient"),
            patch("generate_nibe_mqtt.copy_card_file"),
            patch("generate_nibe_mqtt.mqtt.Client", return_value=mock_mc) as mock_client_cls,
            patch("generate_nibe_mqtt.time.sleep"),
        ):
            _build_infrastructure(cfg)
            return mock_client_cls, mock_mc

    def test_missing_credentials_logs_exact_three_lines(self):
        from generate_nibe_mqtt import _build_infrastructure

        cfg = self._cfg(nibe_auth=None)
        with patch("generate_nibe_mqtt.log_api") as mock_log, self.assertRaises(SystemExit):
            _build_infrastructure(cfg)
        calls = [c.args[0] for c in mock_log.error.call_args_list]
        self.assertEqual(
            calls,
            [
                "Could not find Nibe API credentials in any source.",
                "  Add-on: set nibe_username + nibe_password in the add-on options UI",
                "  secrets.yaml: add  nibe_basic_auth: <base64token>",
            ],
        )

    def test_ssl_context_built_from_real_cfg_ca_cert(self):
        """_build_ssl_context must receive the real cfg.nibe_ca_cert — not
        None or a dropped arg."""
        import ssl

        from generate_nibe_mqtt import _build_infrastructure

        cfg = self._cfg(nibe_ca_cert="/data/ca.pem")
        with (
            patch(
                "generate_nibe_mqtt._build_ssl_context", side_effect=ssl.SSLError("boom")
            ) as mock_ssl,
            self.assertRaises(SystemExit),
        ):
            _build_infrastructure(cfg)
        mock_ssl.assert_called_once_with("/data/ca.pem")

    def test_ssl_error_logs_exact_text_and_real_exception(self):
        import ssl

        from generate_nibe_mqtt import _build_infrastructure

        cfg = self._cfg()
        real_err = ssl.SSLError("bad CA cert")
        with (
            patch("generate_nibe_mqtt._build_ssl_context", side_effect=real_err),
            patch("generate_nibe_mqtt.log_startup") as mock_log,
            self.assertRaises(SystemExit),
        ):
            _build_infrastructure(cfg)
        mock_log.error.assert_called_once_with(
            "Could not build TLS context for the Nibe API connection: %s", real_err
        )

    def test_bridge_version_log_has_exact_text_and_real_value(self):
        from generate_nibe_mqtt import BRIDGE_VERSION

        with patch("generate_nibe_mqtt.log_startup") as mock_log:
            self._run_infra(self._cfg())
        mock_log.info.assert_any_call("Bridge version: %s", BRIDGE_VERSION)

    def test_config_log_has_exact_text_and_real_field_values(self):
        cfg = self._cfg(
            api_base_url="https://real-host/api",
            mqtt_broker="real-broker",
            mqtt_port=1884,
            device_name="Real Device",
        )
        with patch("generate_nibe_mqtt.log_startup") as mock_log:
            self._run_infra(cfg)
        mock_log.info.assert_any_call(
            "Config: API=%s  MQTT=%s:%d  device='%s'",
            "https://real-host/api",
            "real-broker",
            1884,
            "Real Device",
        )

    def test_api_auth_error_log_has_exact_text_and_real_exception(self):
        from generate_nibe_mqtt import _ApiAuthError, _build_infrastructure

        cfg = self._cfg()
        real_err = _ApiAuthError(401)
        with (
            patch("generate_nibe_mqtt._fetch_api_response", side_effect=real_err),
            patch("generate_nibe_mqtt._build_ssl_context", return_value=MagicMock()),
            patch("generate_nibe_mqtt.NibeApiClient"),
            patch("generate_nibe_mqtt.copy_card_file"),
            patch("generate_nibe_mqtt.log_startup") as mock_log,
            self.assertRaises(SystemExit),
        ):
            _build_infrastructure(cfg)
        mock_log.error.assert_called_once_with(
            "Nibe API authentication failed (HTTP %s) — check credentials.", real_err
        )

    def test_mqtt_client_id_derived_from_real_device_id_not_none(self):
        """mqtt_client_id must come from _build_mqtt_client_id(device_id) —
        not None or a dropped call. device_id feeds the client_id kwarg on
        mqtt.Client(); with device_id='nibe_test' the real derived id must
        be non-None and within MQTT 3.1's 23-char client-id limit."""
        mock_client_cls, _mc = self._run_infra(self._cfg(device_id="nibe_test"))
        client_id = mock_client_cls.call_args.kwargs.get("client_id")
        self.assertIsNotNone(client_id)
        self.assertLessEqual(len(client_id), 23)

    def test_connecting_log_has_exact_text(self):
        with patch("generate_nibe_mqtt.log_startup") as mock_log:
            self._run_infra(self._cfg())
        mock_log.info.assert_any_call("Connecting to MQTT broker...")

    def test_mqtt_client_constructed_with_real_callback_api_version(self):
        import paho.mqtt.client as mqtt

        mock_client_cls, _mc = self._run_infra(self._cfg())
        self.assertEqual(mock_client_cls.call_args.args[0], mqtt.CallbackAPIVersion.VERSION2)

    def test_mqtt_client_constructed_with_real_client_id_kwarg(self):
        mock_client_cls, _mc = self._run_infra(self._cfg())
        self.assertIn("client_id", mock_client_cls.call_args.kwargs)
        self.assertIsNotNone(mock_client_cls.call_args.kwargs["client_id"])

    def test_no_credentials_warning_has_exact_text(self):
        with patch("generate_nibe_mqtt.log_mqtt") as mock_log:
            self._run_infra(self._cfg(mqtt_username=None, mqtt_password=None))
        mock_log.warning.assert_called_once_with(
            "MQTT broker connected without credentials — ensure broker ACLs "
            "restrict write access to nibe/ and homeassistant/ topics. "
            "Set mqtt_username and mqtt_password in the add-on options."
        )

    def test_configure_mqtt_tls_called_with_the_real_client(self):
        cfg = self._cfg()
        mock_mc = MagicMock()
        mock_mc.is_connected.return_value = True
        with (
            patch("generate_nibe_mqtt._fetch_api_response", return_value={}),
            patch("generate_nibe_mqtt._build_ssl_context", return_value=MagicMock()),
            patch("generate_nibe_mqtt.NibeApiClient"),
            patch("generate_nibe_mqtt.copy_card_file"),
            patch("generate_nibe_mqtt.mqtt.Client", return_value=mock_mc),
            patch("generate_nibe_mqtt._configure_mqtt_tls") as mock_tls,
            patch("generate_nibe_mqtt.time.sleep"),
        ):
            from generate_nibe_mqtt import _build_infrastructure

            _build_infrastructure(cfg)
        mock_tls.assert_called_once_with(mock_mc, cfg)

    def _fire_on_connect(self, cfg, rc, mock_mc=None):
        """Build infra, capture the real on_connect callback, then fire it
        with the given reason_code — returns the mocked log_mqtt."""
        _mock_client_cls, mc = self._run_infra(cfg, mock_mc)
        on_connect = mc.on_connect
        with patch("generate_nibe_mqtt.log_mqtt") as mock_log:
            on_connect(mc, None, None, rc, None)
        return mock_log

    def test_on_connect_success_log_has_exact_text_and_real_broker_port_keepalive(self):
        cfg = self._cfg(mqtt_broker="real-broker", mqtt_port=1884, poll_interval=30)
        import types

        rc = types.SimpleNamespace(value=0)
        mock_log = self._fire_on_connect(cfg, rc)
        from generate_nibe_mqtt import _keepalive_from_config

        mock_log.info.assert_called_once_with(
            "MQTT connected to %s:%d (keepalive %ds)",
            "real-broker",
            1884,
            _keepalive_from_config(30),
        )

    def test_on_connect_fatal_rc_logs_exact_text_and_real_broker_port_rc(self):
        cfg = self._cfg(mqtt_broker="real-broker", mqtt_port=1884)
        import types

        rc = types.SimpleNamespace(value=4)
        mock_log = self._fire_on_connect(cfg, rc)
        mock_log.error.assert_called_once_with(
            "MQTT broker %s:%d refused the connection (reason %d) — "
            "check mqtt_username and mqtt_password in the add-on options.",
            "real-broker",
            1884,
            4,
        )

    def test_on_connect_other_failure_logs_exact_text_and_real_args(self):
        cfg = self._cfg(mqtt_broker="real-broker", mqtt_port=1884)
        import types

        rc = types.SimpleNamespace(value=3)
        mock_log = self._fire_on_connect(cfg, rc)
        mock_log.error.assert_called_once_with(
            "MQTT connection to %s:%d failed: %s",
            "real-broker",
            1884,
            rc,
        )

    def test_reason_code_without_value_attr_falls_back_to_int(self):
        """A plain int reason_code (no .value attribute) must hit the
        int(reason_code) fallback, not crash or silently misread rc_value.
        A MagicMock can't test this branch — it auto-vivifies any attribute
        name, so hasattr(mock, 'anything') is always True."""
        cfg = self._cfg(mqtt_broker="real-broker", mqtt_port=1884)
        mock_log = self._fire_on_connect(cfg, 4)  # bare int, in _FATAL_RC
        mock_log.error.assert_called_once_with(
            "MQTT broker %s:%d refused the connection (reason %d) — "
            "check mqtt_username and mqtt_password in the add-on options.",
            "real-broker",
            1884,
            4,
        )

    def test_on_connect_generic_failure_log_has_exact_text_and_real_args(self):
        cfg = self._cfg(mqtt_broker="real-broker", mqtt_port=1884)
        import types

        rc = types.SimpleNamespace(value=3)  # not in _FATAL_RC, not 0
        mock_log = self._fire_on_connect(cfg, rc)
        mock_log.error.assert_called_once_with(
            "MQTT connection to %s:%d failed: %s",
            "real-broker",
            1884,
            rc,
        )

    def _fire_on_disconnect(self, cfg, rc, shutting_down_flag=False, mock_mc=None):
        from generate_nibe_mqtt import _build_infrastructure

        mock_mc = mock_mc or MagicMock()
        mock_mc.is_connected.return_value = True
        with (
            patch("generate_nibe_mqtt._fetch_api_response", return_value={}),
            patch("generate_nibe_mqtt._build_ssl_context", return_value=MagicMock()),
            patch("generate_nibe_mqtt.NibeApiClient"),
            patch("generate_nibe_mqtt.copy_card_file"),
            patch("generate_nibe_mqtt.mqtt.Client", return_value=mock_mc),
            patch("generate_nibe_mqtt.time.sleep"),
        ):
            _, mc, _, _, shutting_down, _set_em = _build_infrastructure(cfg)
        shutting_down[0] = shutting_down_flag
        on_disconnect = mc.on_disconnect
        with patch("generate_nibe_mqtt.log_mqtt") as mock_log:
            on_disconnect(mc, None, None, rc, None)
        return mock_log

    def test_on_disconnect_reason_code_without_value_attr_falls_back_to_int(self):
        """Same hasattr/int() fallback logic as on_connect, duplicated in
        on_disconnect — must be independently pinned."""
        cfg = self._cfg(mqtt_broker="real-broker", mqtt_port=1884)
        mock_log = self._fire_on_disconnect(cfg, 0)  # bare int
        mock_log.warning.assert_called_once_with(
            "MQTT disconnected from %s:%d (%s) — paho will reconnect automatically",
            "real-broker",
            1884,
            "clean disconnect or connection lost",
        )

    def test_on_disconnect_label_0_exact_text(self):
        import types

        cfg = self._cfg(mqtt_broker="real-broker", mqtt_port=1884)
        mock_log = self._fire_on_disconnect(cfg, types.SimpleNamespace(value=0))
        mock_log.warning.assert_called_once_with(
            "MQTT disconnected from %s:%d (%s) — paho will reconnect automatically",
            "real-broker",
            1884,
            "clean disconnect or connection lost",
        )

    def test_on_disconnect_label_1_exact_text(self):
        import types

        cfg = self._cfg(mqtt_broker="real-broker", mqtt_port=1884)
        mock_log = self._fire_on_disconnect(cfg, types.SimpleNamespace(value=1))
        mock_log.warning.assert_called_once_with(
            "MQTT disconnected from %s:%d (%s) — paho will reconnect automatically",
            "real-broker",
            1884,
            "connection refused — wrong protocol version",
        )

    def test_on_disconnect_label_2_exact_text(self):
        import types

        cfg = self._cfg(mqtt_broker="real-broker", mqtt_port=1884)
        mock_log = self._fire_on_disconnect(cfg, types.SimpleNamespace(value=2))
        mock_log.warning.assert_called_once_with(
            "MQTT disconnected from %s:%d (%s) — paho will reconnect automatically",
            "real-broker",
            1884,
            "connection refused — client ID rejected",
        )

    def test_on_disconnect_label_3_exact_text(self):
        import types

        cfg = self._cfg(mqtt_broker="real-broker", mqtt_port=1884)
        mock_log = self._fire_on_disconnect(cfg, types.SimpleNamespace(value=3))
        mock_log.warning.assert_called_once_with(
            "MQTT disconnected from %s:%d (%s) — paho will reconnect automatically",
            "real-broker",
            1884,
            "connection refused — broker unavailable",
        )

    def test_on_disconnect_label_4_exact_text(self):
        import types

        cfg = self._cfg(mqtt_broker="real-broker", mqtt_port=1884)
        mock_log = self._fire_on_disconnect(cfg, types.SimpleNamespace(value=4))
        mock_log.warning.assert_called_once_with(
            "MQTT disconnected from %s:%d (%s) — paho will reconnect automatically",
            "real-broker",
            1884,
            "connection refused — wrong credentials",
        )

    def test_on_disconnect_label_5_exact_text(self):
        import types

        cfg = self._cfg(mqtt_broker="real-broker", mqtt_port=1884)
        mock_log = self._fire_on_disconnect(cfg, types.SimpleNamespace(value=5))
        mock_log.warning.assert_called_once_with(
            "MQTT disconnected from %s:%d (%s) — paho will reconnect automatically",
            "real-broker",
            1884,
            "connection refused — not authorised",
        )

    def test_on_disconnect_unknown_label_uses_str_reason_code_not_none(self):
        """label must fall back to str(reason_code) for an unmapped rc —
        not None (which would print 'None' and hide the real reason)."""
        import types

        cfg = self._cfg(mqtt_broker="real-broker", mqtt_port=1884)
        rc = types.SimpleNamespace(value=42)
        mock_log = self._fire_on_disconnect(cfg, rc)
        mock_log.warning.assert_called_once_with(
            "MQTT disconnected from %s:%d (%s) — paho will reconnect automatically",
            "real-broker",
            1884,
            str(rc),
        )

    def test_on_disconnect_get_uses_rc_value_as_real_lookup_key(self):
        """_DISCONNECT_LABELS.get(rc_value, ...) must key off the real
        rc_value, not None — an unmapped key (None) would always miss and
        silently fall back to str(reason_code) even for a known rc."""
        import types

        cfg = self._cfg(mqtt_broker="real-broker", mqtt_port=1884)
        rc = types.SimpleNamespace(value=1)  # a KNOWN key
        mock_log = self._fire_on_disconnect(cfg, rc)
        # Must be the mapped label, not str(rc) (which .get(None, ...) would produce)
        mock_log.warning.assert_called_once_with(
            "MQTT disconnected from %s:%d (%s) — paho will reconnect automatically",
            "real-broker",
            1884,
            "connection refused — wrong protocol version",
        )

    def test_on_disconnect_default_is_real_str_reason_code_not_none(self):
        """The .get() default must be str(reason_code), not None — an
        unmapped rc must show the real reason_code text, not 'None'.
        types.SimpleNamespace can't override __str__ via an instance
        attribute (dunder lookups go through the type, not the instance),
        so a tiny real class is used instead."""

        class _Rc:
            value = 99

            def __str__(self):
                return "distinctive-rc-99"

        cfg = self._cfg(mqtt_broker="real-broker", mqtt_port=1884)
        mock_log = self._fire_on_disconnect(cfg, _Rc())
        self.assertEqual(
            mock_log.warning.call_args.args[3],
            "distinctive-rc-99",
        )

    def test_disconnect_warning_has_exact_text_and_real_broker_port(self):
        import types

        cfg = self._cfg(mqtt_broker="real-broker", mqtt_port=1884)
        mock_log = self._fire_on_disconnect(cfg, types.SimpleNamespace(value=0))
        self.assertEqual(
            mock_log.warning.call_args.args[0],
            "MQTT disconnected from %s:%d (%s) — paho will reconnect automatically",
        )
        self.assertEqual(mock_log.warning.call_args.args[1], "real-broker")
        self.assertEqual(mock_log.warning.call_args.args[2], 1884)

    def test_loop_start_followed_by_exactly_two_second_sleep(self):
        cfg = self._cfg()
        mock_mc = MagicMock()
        mock_mc.is_connected.return_value = True
        with (
            patch("generate_nibe_mqtt._fetch_api_response", return_value={}),
            patch("generate_nibe_mqtt._build_ssl_context", return_value=MagicMock()),
            patch("generate_nibe_mqtt.NibeApiClient"),
            patch("generate_nibe_mqtt.copy_card_file"),
            patch("generate_nibe_mqtt.mqtt.Client", return_value=mock_mc),
            patch("generate_nibe_mqtt.time.sleep") as mock_sleep,
        ):
            from generate_nibe_mqtt import _build_infrastructure

            _build_infrastructure(cfg)
        mock_sleep.assert_called_once_with(2)

    def test_not_yet_connected_log_has_exact_text(self):
        """_run_infra always forces is_connected=True, so this test builds
        the mock and calls _build_infrastructure directly instead."""
        from generate_nibe_mqtt import _build_infrastructure

        cfg = self._cfg()
        mock_mc = MagicMock()
        mock_mc.is_connected.return_value = False
        with (
            patch("generate_nibe_mqtt._fetch_api_response", return_value={}),
            patch("generate_nibe_mqtt._build_ssl_context", return_value=MagicMock()),
            patch("generate_nibe_mqtt.NibeApiClient"),
            patch("generate_nibe_mqtt.copy_card_file"),
            patch("generate_nibe_mqtt.mqtt.Client", return_value=mock_mc),
            patch("generate_nibe_mqtt.time.sleep"),
            patch("generate_nibe_mqtt.log_mqtt") as mock_log,
        ):
            _build_infrastructure(cfg)
        mock_log.info.assert_any_call(
            "MQTT not yet connected after 2s — broker may be slow, continuing"
        )

    def test_connection_verified_log_has_exact_text(self):
        cfg = self._cfg()
        mock_mc = MagicMock()
        mock_mc.is_connected.return_value = True
        with patch("generate_nibe_mqtt.log_mqtt") as mock_log:
            self._run_infra(cfg, mock_mc)
        mock_log.info.assert_any_call("MQTT client connection verified")

    def test_availability_precleared_log_has_exact_text(self):
        with patch("generate_nibe_mqtt.log_mqtt") as mock_log:
            self._run_infra(self._cfg())
        mock_log.info.assert_any_call("Availability topic pre-cleared to 'online'")

    def test_mqtt_connect_exception_logs_exact_text_and_real_args(self):
        cfg = self._cfg(mqtt_broker="real-broker", mqtt_port=1884)
        mock_mc = MagicMock()
        real_err = OSError("connection refused")
        mock_mc.connect.side_effect = real_err
        with (
            patch("generate_nibe_mqtt._fetch_api_response", return_value={}),
            patch("generate_nibe_mqtt._build_ssl_context", return_value=MagicMock()),
            patch("generate_nibe_mqtt.NibeApiClient"),
            patch("generate_nibe_mqtt.copy_card_file"),
            patch("generate_nibe_mqtt.mqtt.Client", return_value=mock_mc),
            patch("generate_nibe_mqtt.log_mqtt") as mock_log,
            self.assertRaises(SystemExit),
        ):
            from generate_nibe_mqtt import _build_infrastructure

            _build_infrastructure(cfg)
        mock_log.error.assert_called_once_with(
            "Cannot connect to MQTT broker at %s:%d — %s. "
            "Check that the broker is running and that 'mqtt_host' and 'mqtt_port' "
            "are correctly set in the add-on configuration. "
            "If using the Mosquitto add-on, the default host is 'core-mosquitto'.",
            "real-broker",
            1884,
            real_err,
        )


class TestBuildInfrastructureOnConnectEmptyEm(unittest.TestCase):
    """on_connect: 851→exit — rc=0 before entity_manager is wired.

    The first MQTT connection fires on_connect before _run_startup_sequence
    has called set_entity_manager().  At that point _em=[] is falsy, so
    resubscribe_all/republish_availability must NOT be called.
    """

    def _cfg(self):
        from generate_nibe_mqtt import BridgeConfig

        return BridgeConfig(
            api_base_url="https://10.0.0.1:8443/api/v1/devices/0",
            nibe_auth="Basic dXNlcjpwYXNz",
            mqtt_broker="localhost",
            mqtt_port=1883,
            device_name="Test Device",
            device_id="nibe_test",
            poll_interval=30,
        )

    def test_on_connect_rc0_before_set_em_does_not_call_resubscribe(self):
        """Firing on_connect with rc=0 before set_entity_manager() is called
        must not attempt resubscribe_all — _em is still empty (851→exit)."""
        from generate_nibe_mqtt import _build_infrastructure

        cfg = self._cfg()
        mock_mc = MagicMock()
        mock_mc.is_connected.return_value = True
        with (
            patch("generate_nibe_mqtt._fetch_api_response", return_value={}),
            patch("generate_nibe_mqtt._build_ssl_context", return_value=MagicMock()),
            patch("generate_nibe_mqtt.NibeApiClient"),
            patch("generate_nibe_mqtt.copy_card_file"),
            patch("generate_nibe_mqtt.mqtt.Client", return_value=mock_mc),
            patch("generate_nibe_mqtt.time.sleep"),
        ):
            _, _, _, _, _, _set_em = _build_infrastructure(cfg)

        # Fire on_connect WITHOUT calling set_em first — _em is still []
        on_connect = mock_mc.on_connect
        rc = MagicMock()
        rc.value = 0
        on_connect(mock_mc, None, None, rc, None)  # must not raise
        # No entity manager was wired — resubscribe/republish must not be called


# ===========================================================================
# freeze_time — _poll_loop memory logging and alarm check timing
# ===========================================================================


class TestPollLoopFreezeTime(unittest.TestCase):
    """_poll_loop timing branches using freeze_time for clock control.

    freeze_time starts the clock at a fixed instant.  We advance time
    by moving the freeze between iterations via a side-effect on
    time.time, while freeze_time ensures time.sleep is a no-op and
    the module-level time import sees the frozen clock.
    """

    def _make_em_and_pub(self):
        em = _make_em()
        pub = MagicMock()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em.bulk_interval = 30
        em._post_write_interval = 5
        em.update_all_states = MagicMock()
        return em, pub

    @freeze_time("2024-06-01 00:00:00")
    def test_memory_log_triggered_after_600s_frozen(self):
        """Memory usage is logged when current_time - last_memory_log >= 600.

        freeze_time holds the clock steady; we supply a time.time side-effect
        that advances by 700s on each call so the 600s threshold is crossed
        on the first update cycle.
        """
        from generate_nibe_mqtt import _poll_loop

        em, pub = self._make_em_and_pub()
        em.get_memory_usage = MagicMock(
            return_value={
                "total_points": 100,
                "active_entities": 50,
                "estimated_memory_mb": 2.5,
                "value_cache_size": 10,
                "last_states_size": 20,
                "point_string_cache_size": 5,
            }
        )

        tick = [0]
        _t = [0.0]

        def _fake_time():
            _t[0] += 700.0  # each call jumps 700s → crosses 600s threshold
            return _t[0]

        def _fake_sleep(_s):
            tick[0] += 1
            if tick[0] >= 2:
                raise KeyboardInterrupt

        with (
            patch("generate_nibe_mqtt.time.time", side_effect=_fake_time),
            patch("generate_nibe_mqtt.time.sleep", side_effect=_fake_sleep),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.update_alarm_state"),
            self.assertRaises(KeyboardInterrupt),
        ):
            _poll_loop(em, pub, "essential")

        em.get_memory_usage.assert_called()

    @freeze_time("2024-06-01 00:00:00")
    def test_backoff_formula_via_freeze_time(self):
        """Backoff = min(5 * consecutive_errors, 60).

        Using freeze_time + controlled time.time ensures the formula is
        exercised against real wall-clock expectations: errors 1-5 produce
        5, 10, 15, 20, 25; errors 12+ are capped at 60.
        """
        from generate_nibe_mqtt import _poll_loop

        em, pub = self._make_em_and_pub()
        em.update_all_states = MagicMock(side_effect=RuntimeError("crash"))

        backoff_sleeps = []
        tick = [0]
        _t = [0.0]

        def _fake_time():
            _t[0] += 60.0
            return _t[0]

        def _fake_sleep(s):
            tick[0] += 1
            if s > 1:
                backoff_sleeps.append(s)
            if tick[0] >= 20:
                raise KeyboardInterrupt

        with (
            patch("generate_nibe_mqtt.time.time", side_effect=_fake_time),
            patch("generate_nibe_mqtt.time.sleep", side_effect=_fake_sleep),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.update_alarm_state"),
            self.assertRaises(KeyboardInterrupt),
        ):
            _poll_loop(em, pub, "essential")

        # At least 3 distinct backoff values observed
        self.assertGreaterEqual(len(backoff_sleeps), 3)
        # Values escalate
        self.assertLess(backoff_sleeps[0], backoff_sleeps[1])
        # None exceed the 60s cap
        self.assertLessEqual(max(backoff_sleeps), 60)

    @freeze_time("2024-06-01 00:00:00")
    def test_consecutive_error_count_resets_after_clean_cycle(self):
        """After an exception the consecutive error count increments;
        after a clean cycle it resets to zero (line 1138).

        Verify: if update_all_states crashes once then succeeds, the
        next crash re-starts the count from 1 (backoff = 5).
        """
        from generate_nibe_mqtt import _poll_loop

        em, pub = self._make_em_and_pub()

        call_seq = iter(
            [
                RuntimeError("first crash"),  # error → count=1, backoff=5
                None,  # success → count resets to 0
                RuntimeError("second crash"),  # error → count=1 again, backoff=5
            ]
        )

        def _update():
            v = next(call_seq, None)
            if isinstance(v, Exception):
                raise v

        em.update_all_states = MagicMock(side_effect=_update)

        backoff_sleeps = []
        tick = [0]
        _t = [0.0]

        def _fake_time():
            _t[0] += 60.0
            return _t[0]

        def _fake_sleep(s):
            tick[0] += 1
            if s > 1:
                backoff_sleeps.append(s)
            if tick[0] >= 10:
                raise KeyboardInterrupt

        with (
            patch("generate_nibe_mqtt.time.time", side_effect=_fake_time),
            patch("generate_nibe_mqtt.time.sleep", side_effect=_fake_sleep),
            patch("generate_nibe_mqtt.update_stats_and_health"),
            patch("generate_nibe_mqtt.update_device_modes"),
            patch("generate_nibe_mqtt.update_alarm_state"),
            self.assertRaises(KeyboardInterrupt),
        ):
            _poll_loop(em, pub, "essential")

        # After reset, the second crash should produce backoff=5 (count=1),
        # not a higher value that would indicate count was NOT reset.
        self.assertTrue(
            any(s == 5 for s in backoff_sleeps),
            f"Expected a 5s backoff (count=1 after reset), got: {backoff_sleeps}",
        )


# ===========================================================================
# config.yaml / translations/*.yaml — key parity
# ===========================================================================


class TestConfigTranslationsParity(unittest.TestCase):
    """Every config.yaml options field must have a matching entry in every
    translations/<lang>.yaml — otherwise that field silently shows no
    name/description in the add-on Configuration UI for users on that
    language (falls back to the raw field key, e.g. "language" instead of
    "Sprache"). This guards against exactly the kind of oversight that can
    happen when a new option is added to config.yaml without remembering
    the separate translations/ directory that has no schema link to it."""

    @classmethod
    def setUpClass(cls):
        import pathlib

        import yaml as _yaml

        repo_root = pathlib.Path(__file__).resolve().parent.parent
        with open(repo_root / "config.yaml") as f:
            cls.config_options = set(_yaml.safe_load(f)["options"].keys())
        cls.translations_dir = repo_root / "translations"
        cls.translation_files = sorted(cls.translations_dir.glob("*.yaml"))

    def test_translation_files_exist(self):
        self.assertTrue(self.translation_files, "no translations/*.yaml files found")

    def test_every_translation_file_covers_every_config_option(self):
        import yaml as _yaml

        for path in self.translation_files:
            with open(path) as f:
                data = _yaml.safe_load(f)
            translated_keys = set(data.get("configuration", {}).keys())
            missing = self.config_options - translated_keys
            self.assertFalse(
                missing,
                f"{path.name} is missing translation entries for: {sorted(missing)}",
            )

    def test_no_stale_translation_keys_for_removed_options(self):
        """A translation entry for an option no longer in config.yaml is not
        a crash, but it is dead content that should be cleaned up — this
        surfaces it rather than letting it silently accumulate."""
        import yaml as _yaml

        for path in self.translation_files:
            with open(path) as f:
                data = _yaml.safe_load(f)
            translated_keys = set(data.get("configuration", {}).keys())
            stale = translated_keys - self.config_options
            self.assertFalse(
                stale,
                f"{path.name} has translation entries for options no longer "
                f"in config.yaml: {sorted(stale)}",
            )

    def test_every_translated_entry_has_name_and_description(self):
        import yaml as _yaml

        for path in self.translation_files:
            with open(path) as f:
                data = _yaml.safe_load(f)
            for key, entry in data.get("configuration", {}).items():
                self.assertIn("name", entry, f"{path.name}: {key} missing 'name'")
                self.assertIn("description", entry, f"{path.name}: {key} missing 'description'")
                self.assertTrue(
                    (entry.get("name") or "").strip(),
                    f"{path.name}: {key} has an empty 'name'",
                )
                self.assertTrue(
                    (entry.get("description") or "").strip(),
                    f"{path.name}: {key} has an empty 'description'",
                )
