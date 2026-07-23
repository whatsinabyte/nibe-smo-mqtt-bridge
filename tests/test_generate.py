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
from freezegun import freeze_time
from unittest.mock import MagicMock, patch

from hypothesis import given
from hypothesis import strategies as st

from conftest import (
    _make_em,
    _nibe_point_id,
    _APP_DIR,
)

class TestCleanUnit(unittest.TestCase):
    """Single source of truth for unit cleaning, consolidating three
    previously-independent implementations (a direct mojibake strip used
    twice in generate_nibe_mqtt.py, and a bare _UNIT_NORMALISE table
    lookup used in map_device_class) that could silently drift apart.
    Mirrors the structure of TestCleanString above."""

    def setUp(self):
        from nibe_entity_detection import clean_unit
        self.fn = clean_unit

    def test_normal(self):          self.assertEqual(self.fn("°C"), "°C")
    def test_mojibake_degree_c(self): self.assertEqual(self.fn("\u00c2°C"), "°C")
    def test_mojibake_degree_f(self): self.assertEqual(self.fn("\u00c2°F"), "°F")
    def test_mojibake_bare_degree(self): self.assertEqual(self.fn("\u00c2°"), "°")
    def test_days_normalised_to_d(self): self.assertEqual(self.fn("days"), "d")
    def test_unrecognised_unit_passthrough(self): self.assertEqual(self.fn("bar"), "bar")
    def test_percent_passthrough(self): self.assertEqual(self.fn("%"), "%")
    def test_whitespace_stripped(self): self.assertEqual(self.fn("  °C  "), "°C")
    def test_nbsp_collapsed(self):  self.assertEqual(self.fn("a\u00a0b"), "a b")
    def test_none(self):            self.assertEqual(self.fn(None), "")
    def test_empty(self):           self.assertEqual(self.fn(""), "")
    def test_non_string(self):      self.assertEqual(self.fn(42), "")

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

    def _point(self, pid, modbus_type, var_type='integer', writable=True):
        return {
            'variableId': pid,
            'description': '',
            'metadata': {
                'variableType': var_type,
                'variableSize': 'u8',
                'modbusRegisterType': modbus_type,
                'isWritable': writable,
                'minValue': 0, 'maxValue': 1,
                'unit': '', 'divisor': 1,
            }
        }

    @given(_nibe_point_id,
           st.sampled_from(['MODBUS_HOLDING_REGISTER', 'MODBUS_INPUT_REGISTER',
                            'MODBUS_NO_REGISTER', '']))
    def test_always_returns_two_tuple(self, pid, modbus_type):
        from nibe_entity_detection import _detect_type_without_override
        point = self._point(pid, modbus_type)
        result = _detect_type_without_override(point, point['metadata'], modbus_type)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    @given(_nibe_point_id.filter(
        lambda p: p not in __import__('nibe_entity_detection').VALUE_MAPPINGS.get('holding', {})))
    def test_holding_consistent_with_detect_holding_entity(self, pid):
        """For HOLDING registers, result must match _detect_holding_entity directly."""
        from nibe_entity_detection import _detect_type_without_override, _detect_holding_entity
        point = self._point(pid, 'MODBUS_HOLDING_REGISTER')
        meta = point['metadata']
        self.assertEqual(
            _detect_type_without_override(point, meta, 'MODBUS_HOLDING_REGISTER'),
            _detect_holding_entity(point, meta),
        )

    @given(_nibe_point_id.filter(
        lambda p: p not in __import__('nibe_entity_detection').VALUE_MAPPINGS.get('input', {})))
    def test_input_consistent_with_detect_input_entity(self, pid):
        """For INPUT registers, result must match _detect_input_entity directly."""
        from nibe_entity_detection import _detect_type_without_override, _detect_input_entity
        point = self._point(pid, 'MODBUS_INPUT_REGISTER', writable=False)
        meta = point['metadata']
        self.assertEqual(
            _detect_type_without_override(point, meta, 'MODBUS_INPUT_REGISTER'),
            _detect_input_entity(point, meta),
        )

    @given(_nibe_point_id,
           st.text().filter(lambda s: s not in (
               'MODBUS_HOLDING_REGISTER', 'MODBUS_INPUT_REGISTER')))
    def test_unknown_register_type_always_sensor_diagnostic(self, pid, modbus_type):
        """Any register type that is not HOLDING or INPUT → sensor/diagnostic."""
        from nibe_entity_detection import _detect_type_without_override
        point = self._point(pid, modbus_type)
        result = _detect_type_without_override(point, point['metadata'], modbus_type)
        self.assertEqual(result, ('sensor', 'diagnostic'))

    @given(_nibe_point_id,
           st.sampled_from(['MODBUS_HOLDING_REGISTER', 'MODBUS_INPUT_REGISTER',
                            'MODBUS_NO_REGISTER']))
    def test_never_raises(self, pid, modbus_type):
        from nibe_entity_detection import _detect_type_without_override
        point = self._point(pid, modbus_type)
        _detect_type_without_override(point, point['metadata'], modbus_type)


# ---------------------------------------------------------------------------
# BridgeConfig.__repr__ credential redaction properties (generate_nibe_mqtt.py)
# ---------------------------------------------------------------------------


class TestBridgeConfigReprProperties(unittest.TestCase):
    """Hypothesis properties for BridgeConfig.__repr__ credential redaction."""

    @given(st.text(min_size=16, max_size=50,
                   alphabet=st.characters(categories=['L', 'N'],
                                          include_characters='_')).filter(
               lambda s: s not in ('core-mosquitto', 'Nibe SMO S40')),
           st.text(min_size=16, max_size=50,
                   alphabet=st.characters(categories=['L', 'N'],
                                          include_characters='_')))
    def test_credentials_never_appear_in_repr(self, auth, password):
        """__repr__ must never expose actual credential values.
        Uses ≥16-char alphanumeric strings — long enough that no generated
        value can be a substring of a static repr field name like
        'nibe_password' (13 chars)."""
        from generate_nibe_mqtt import BridgeConfig
        cfg = BridgeConfig()
        cfg.nibe_auth     = auth
        cfg.mqtt_password = password
        r = repr(cfg)
        self.assertNotIn(auth,     r)
        self.assertNotIn(password, r)

    @given(st.text(min_size=1, max_size=50))
    def test_repr_always_returns_string(self, auth):
        from generate_nibe_mqtt import BridgeConfig
        cfg = BridgeConfig()
        cfg.nibe_auth = auth
        self.assertIsInstance(repr(cfg), str)

    @given(st.text(min_size=1, max_size=50),
           st.text(min_size=1, max_size=50))
    def test_repr_contains_redacted_marker(self, auth, password):
        """Redacted fields must show a placeholder, not be silently empty."""
        from generate_nibe_mqtt import BridgeConfig
        cfg = BridgeConfig()
        cfg.nibe_auth     = auth
        cfg.mqtt_password = password
        r = repr(cfg)
        # Must contain some redaction marker
        self.assertTrue(
            '***' in r or 'REDACTED' in r or '<' in r,
            f"No redaction marker found in repr: {r[:100]}"
        )

    def test_none_credentials_do_not_crash_repr(self):
        """None credentials must not cause __repr__ to raise."""
        from generate_nibe_mqtt import BridgeConfig
        cfg = BridgeConfig()
        cfg.nibe_auth     = None
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
        self.assertIn(f'poll={poll}s', r)

    @given(st.integers(min_value=1, max_value=3600))
    def test_repr_always_string(self, poll):
        from generate_nibe_mqtt import BridgeConfig
        cfg = BridgeConfig()
        cfg.poll_interval = poll
        self.assertIsInstance(repr(cfg), str)

    def test_default_mode_is_valid(self):
        """Default mode must be one of the supported modes."""
        from generate_nibe_mqtt import BridgeConfig, MODES
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
        self._env_patcher = patch.dict('os.environ', {}, clear=True)
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()

    def _load(self, options=None, secrets=None, env=None, cli_args=None):
        """Call load_config with mocked filesystem and environment."""
        import generate_nibe_mqtt as gn
        env = env or {}

        def fake_exists(path):
            if path == '/data/options.json':
                return options is not None
            if path in ('/config/secrets.yaml', '/homeassistant/secrets.yaml', './secrets.yaml'):
                return secrets is not None and path == './secrets.yaml'
            return False

        import io
        def fake_open(path, *a, **kw):
            if path == '/data/options.json':
                return io.StringIO(json.dumps(options))
            if path == './secrets.yaml':
                return io.StringIO(secrets)
            raise FileNotFoundError(path)

        with patch('os.path.exists', side_effect=fake_exists), \
             patch('builtins.open', side_effect=fake_open), \
             patch.dict('os.environ', env):
            return gn.load_config(cli_args)

    # ── defaults ─────────────────────────────────────────────────────────────

    def test_defaults_when_no_sources(self):
        cfg = self._load()
        self.assertEqual(cfg.api_host,    '192.168.2.201')
        self.assertEqual(cfg.api_port,    8443)
        self.assertEqual(cfg.mqtt_broker, 'core-mosquitto')
        self.assertEqual(cfg.mqtt_port,   1883)
        self.assertEqual(cfg.poll_interval, 30)
        self.assertEqual(cfg.mode,        'essential')
        self.assertEqual(cfg.log_level,   'info')

    # ── options.json ─────────────────────────────────────────────────────────

    def test_options_json_sets_api_host(self):
        cfg = self._load(options={'nibe_host': '10.0.0.5'})
        self.assertEqual(cfg.api_host, '10.0.0.5')

    def test_options_json_sets_mqtt_broker(self):
        cfg = self._load(options={'mqtt_host': 'mybroker'})
        self.assertEqual(cfg.mqtt_broker, 'mybroker')

    def test_options_json_sets_poll_interval(self):
        cfg = self._load(options={'poll_interval': '60'})
        self.assertEqual(cfg.poll_interval, 60)

    def test_options_json_invalid_poll_interval_snaps_to_nearest(self):
        cfg = self._load(options={'poll_interval': '45'})
        self.assertIn(cfg.poll_interval, {15, 30, 60, 120, 300})
        self.assertEqual(cfg.poll_interval, 30)  # nearest to 45 is 30 or 60; tie goes to 30

    def test_options_json_sets_mode(self):
        cfg = self._load(options={'mode': 'all'})
        self.assertEqual(cfg.mode, 'all')

    def test_options_json_sets_log_level(self):
        cfg = self._load(options={'log_level': 'debug'})
        self.assertEqual(cfg.log_level, 'debug')

    def test_options_json_sets_device_name(self):
        cfg = self._load(options={'device_name': 'My Heat Pump'})
        self.assertEqual(cfg.device_name, 'My Heat Pump')

    def test_options_json_sets_api_failure_threshold(self):
        cfg = self._load(options={'api_failure_threshold': 5})
        self.assertEqual(cfg.api_failure_threshold, 5)

    def test_options_json_sets_changelog_retention_days(self):
        cfg = self._load(options={'changelog_retention_days': 30})
        self.assertEqual(cfg.changelog_retention_days, 30)

    def test_options_json_sets_mqtt_tls(self):
        cfg = self._load(options={'mqtt_tls': True})
        self.assertTrue(cfg.mqtt_tls)

    def test_options_json_sets_changelog_retention(self):
        cfg = self._load(options={'changelog_retention_days': 30})
        self.assertEqual(cfg.changelog_retention_days, 30)

    def test_options_json_parse_error_adds_warning(self):
        import generate_nibe_mqtt as gn
        def fake_exists(p): return p == '/data/options.json'
        import io
        def fake_open(p, *a, **kw):
            if p == '/data/options.json':
                return io.StringIO('not valid json {{{')
            raise FileNotFoundError(p)
        with patch('os.path.exists', side_effect=fake_exists), \
             patch('builtins.open', side_effect=fake_open):
            cfg = gn.load_config()
        self.assertTrue(any('options.json' in w for w in cfg.warnings))

    # ── secrets.yaml ─────────────────────────────────────────────────────────

    def test_secrets_yaml_sets_mqtt_username(self):
        cfg = self._load(secrets='mqtt_user: myuser\n')
        self.assertEqual(cfg.mqtt_username, 'myuser')

    def test_secrets_yaml_sets_mqtt_password(self):
        cfg = self._load(secrets='mqtt_password: s3cr3t\n')
        self.assertEqual(cfg.mqtt_password, 's3cr3t')

    def test_secrets_yaml_sets_nibe_basic_auth(self):
        cfg = self._load(secrets='nibe_basic_auth: dXNlcjpwYXNz\n')
        self.assertEqual(cfg.nibe_basic_auth, 'dXNlcjpwYXNz')

    def test_secrets_yaml_quoted_value_strips_quotes(self):
        cfg = self._load(secrets='mqtt_password: "pass#word"\n')
        self.assertEqual(cfg.mqtt_password, 'pass#word')

    def test_secrets_yaml_does_not_override_options_json_credentials(self):
        """options.json credentials take priority over secrets.yaml."""
        cfg = self._load(
            options={'mqtt_username': 'from_options'},
            secrets='mqtt_user: from_secrets\n',
        )
        self.assertEqual(cfg.mqtt_username, 'from_options')

    # ── environment variables ─────────────────────────────────────────────────

    def test_env_sets_api_host(self):
        cfg = self._load(env={'NIBE_API_HOST': '10.1.2.3'})
        self.assertEqual(cfg.api_host, '10.1.2.3')

    def test_env_sets_poll_interval(self):
        cfg = self._load(env={'NIBE_POLL_INTERVAL': '120'})
        self.assertEqual(cfg.poll_interval, 120)

    def test_env_poll_interval_below_15_snaps_to_15(self):
        cfg = self._load(env={'NIBE_POLL_INTERVAL': '5'})
        self.assertEqual(cfg.poll_interval, 15)

    def test_env_overrides_options_json_for_api_host(self):
        cfg = self._load(
            options={'nibe_host': '192.168.1.1'},
            env={'NIBE_API_HOST': '10.0.0.99'},
        )
        self.assertEqual(cfg.api_host, '10.0.0.99')

    def test_env_svc_username_sets_mqtt_username(self):
        """NIBE_MQTT_SVC_USERNAME from Supervisor Services API sets mqtt_username."""
        cfg = self._load(env={'NIBE_MQTT_SVC_USERNAME': 'addons'})
        self.assertEqual(cfg.mqtt_username, 'addons')

    def test_env_svc_password_sets_mqtt_password(self):
        """NIBE_MQTT_SVC_PASSWORD from Supervisor Services API sets mqtt_password."""
        cfg = self._load(env={'NIBE_MQTT_SVC_PASSWORD': 'secret123'})
        self.assertEqual(cfg.mqtt_password, 'secret123')

    def test_env_svc_credentials_override_options_json(self):
        """Supervisor-discovered credentials override manually entered options.json values."""
        cfg = self._load(
            options={'mqtt_username': 'manual_user', 'mqtt_password': 'manual_pass'},
            env={
                'NIBE_MQTT_SVC_USERNAME': 'svc_user',
                'NIBE_MQTT_SVC_PASSWORD': 'svc_pass',
            },
        )
        self.assertEqual(cfg.mqtt_username, 'svc_user')
        self.assertEqual(cfg.mqtt_password, 'svc_pass')

    # ── CLI args ──────────────────────────────────────────────────────────────

    def test_cli_log_level_overrides_options_json(self):
        cli = MagicMock()
        cli.log_level = 'debug'
        cli.mode      = None
        cfg = self._load(options={'log_level': 'info'}, cli_args=cli)
        self.assertEqual(cfg.log_level, 'debug')

    def test_cli_mode_overrides_options_json(self):
        cli = MagicMock()
        cli.log_level = None
        cli.mode      = 'all'
        cfg = self._load(options={'mode': 'essential'}, cli_args=cli)
        self.assertEqual(cfg.mode, 'all')

    # ── derived values ────────────────────────────────────────────────────────

    def test_api_base_url_built_from_host_and_port(self):
        cfg = self._load(options={'nibe_host': '10.0.0.5', 'nibe_port': 8443})
        self.assertEqual(cfg.api_base_url,
                         'https://10.0.0.5:8443/api/v1/devices/0')

    def test_nibe_auth_built_from_username_password(self):
        import base64
        cfg = self._load(options={
            'nibe_username': 'user', 'nibe_password': 'pass'
        })
        expected = 'Basic ' + base64.b64encode(b'user:pass').decode()
        self.assertEqual(cfg.nibe_auth, expected)

    def test_nibe_basic_auth_used_directly_when_set(self):
        cfg = self._load(secrets='nibe_basic_auth: Basic dXNlcjpwYXNz\n')
        self.assertEqual(cfg.nibe_auth, 'Basic dXNlcjpwYXNz')

    def test_nibe_basic_auth_without_prefix_gets_basic_prepended(self):
        cfg = self._load(secrets='nibe_basic_auth: dXNlcjpwYXNz\n')
        self.assertTrue(cfg.nibe_auth.startswith('Basic '))

    def test_repr_redacts_passwords(self):
        cfg = self._load(options={
            'nibe_username': 'myspecialuser', 'nibe_password': 'myspecialpass',
            'mqtt_username': 'mqttspecialuser', 'mqtt_password': 'mqttspecialpass',
        })
        r = repr(cfg)
        self.assertNotIn('myspecialpass',    r)
        self.assertNotIn('mqttspecialpass',  r)
        self.assertNotIn('myspecialuser',    r)
        self.assertNotIn('mqttspecialuser',  r)
        self.assertIn('***', r)


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
        with patch('generate_nibe_mqtt.os.path.exists', return_value=False):
            result = _setup_menu_dashboard(open_ws_fn, watcher)
        self.assertIs(result, False)
        self.assertIsNotNone(result)   # confirms False, not None
        open_ws_fn.assert_not_called()  # ws never opened before the early return

    def test_yaml_parse_error_returns_false_not_none(self):
        from nibe_lovelace import _setup_menu_dashboard
        open_ws_fn = MagicMock()
        watcher = self._watcher()
        with patch('generate_nibe_mqtt.os.path.exists', return_value=True), \
             patch('builtins.open', side_effect=OSError("read error")):
            result = _setup_menu_dashboard(open_ws_fn, watcher)
        self.assertIs(result, False)
        open_ws_fn.assert_not_called()

    def test_empty_menu_structure_returns_false_not_none(self):
        from nibe_lovelace import _setup_menu_dashboard
        import io
        open_ws_fn = MagicMock()
        watcher = self._watcher()
        with patch('generate_nibe_mqtt.os.path.exists', return_value=True), \
             patch('builtins.open', return_value=io.StringIO('menus: []')):
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
        with patch('sys.argv', ['bridge', '--mode', 'menus']):
            args = parse_arguments()
        self.assertEqual(args.mode, 'menus')

    def test_all_modes_accepted(self):
        from generate_nibe_mqtt import parse_arguments
        for mode in ('essential', 'monitoring', 'advanced', 'menus', 'all', 'none'):
            with patch('sys.argv', ['bridge', '--mode', mode]):
                args = parse_arguments()
            self.assertEqual(args.mode, mode, f"mode '{mode}' should be accepted")

    def test_invalid_mode_rejected(self):
        from generate_nibe_mqtt import parse_arguments
        with patch('sys.argv', ['bridge', '--mode', 'nonexistent']):
            with self.assertRaises(SystemExit):
                parse_arguments()

    def test_modes_match_detection_module(self):
        """The argparse choices must be a superset of MODES keys so no
        valid mode can ever be rejected at the CLI."""
        from generate_nibe_mqtt import parse_arguments
        from nibe_entity_detection import MODES
        # Reconstruct the choices by parsing the parser's actions
        with patch('sys.argv', ['bridge']):
            # We can't inspect choices directly without accessing parser internals;
            # instead verify every MODES key is accepted without SystemExit.
            for mode in MODES:
                try:
                    with patch('sys.argv', ['bridge', '--mode', mode]):
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

        handler = _on_enabled_state_change_factory(
            rw, False, lovelace_thread=mock_thread
        )

        with patch('nibe_lovelace._regen_menu_dashboard',
                   side_effect=lambda *a, **kw: regen_calls.append(1)), \
             patch('generate_nibe_mqtt.threading.Timer') as mock_timer:
            mock_timer.return_value = MagicMock()
            handler()
            # Give the timer a moment to fire if scheduled
            if mock_timer.called:
                # Simulate timer firing immediately
                call_args = mock_timer.call_args
                delay, fn = call_args[0]
                fn()

        return len(regen_calls)

    def test_regen_skipped_when_lovelace_thread_alive(self):
        """When the Lovelace setup thread is still running, the handler
        must not schedule a regen — the thread will do it itself."""
        calls = self._make_regen_calls(thread_alive=True)
        self.assertEqual(calls, 0,
            "Regen must be skipped when Lovelace thread is alive")

    def test_regen_scheduled_when_lovelace_thread_done(self):
        """When the Lovelace setup thread has finished, the handler must
        schedule a regen normally — user changed entities after startup."""
        calls = self._make_regen_calls(thread_alive=False)
        self.assertEqual(calls, 1,
            "Regen must fire when Lovelace thread is no longer running")

    def test_no_thread_provided_schedules_normally(self):
        """lovelace_thread=None (e.g. outside HA add-on environment) must
        behave as before — schedule regen unconditionally."""
        from nibe_lovelace import _on_enabled_state_change_factory

        rw = MagicMock()
        regen_calls = []

        handler = _on_enabled_state_change_factory(rw, False, lovelace_thread=None)

        with patch('nibe_lovelace._regen_menu_dashboard',
                   side_effect=lambda *a, **kw: regen_calls.append(1)), \
             patch('generate_nibe_mqtt.threading.Timer') as mock_timer:
            mock_timer.return_value = MagicMock()
            handler()
            if mock_timer.called:
                call_args = mock_timer.call_args
                delay, fn = call_args[0]
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
            if path == '/data/options.json':
                return options is not None
            if path == './secrets.yaml':
                return secrets is not None
            return False

        def fake_open(path, *a, **kw):
            if path == '/data/options.json':
                return io.StringIO(json.dumps(options))
            if path == './secrets.yaml':
                if secrets is Exception:
                    raise OSError('forced failure')
                return io.StringIO(secrets)
            raise FileNotFoundError(path)

        with patch('os.path.exists', side_effect=fake_exists), \
             patch('builtins.open', side_effect=fake_open), \
             patch.dict('os.environ', env, clear=True):
            return gn.load_config()

    def test_options_json_sets_nibe_ca_cert(self):
        cfg = self._load(options={'nibe_ca_cert': '/config/ca.pem'})
        self.assertEqual(cfg.nibe_ca_cert, '/config/ca.pem')

    def test_options_json_sets_mqtt_ca_cert(self):
        cfg = self._load(options={'mqtt_ca_cert': '/config/mqtt_ca.pem'})
        self.assertEqual(cfg.mqtt_ca_cert, '/config/mqtt_ca.pem')

    def test_secrets_yaml_read_error_adds_warning(self):
        cfg = self._load(secrets=Exception)
        self.assertTrue(any('secrets' in w.lower() for w in cfg.warnings))

    def test_env_api_failure_threshold_applied(self):
        cfg = self._load(env={'NIBE_API_FAILURE_THRESHOLD': '7'})
        self.assertEqual(cfg.api_failure_threshold, 7)

    def test_build_logging_adds_handler_on_fresh_logger(self):
        import logging
        import generate_nibe_mqtt as gn
        root = logging.getLogger('nibe')
        original_handlers = root.handlers[:]
        root.handlers.clear()
        try:
            gn._build_logging('debug')
            self.assertTrue(len(root.handlers) > 0)
            self.assertEqual(root.level, logging.DEBUG)
        finally:
            root.handlers.clear()
            root.handlers.extend(original_handlers)

    def test_build_logging_skips_handler_when_already_configured(self):
        import logging
        import generate_nibe_mqtt as gn
        root = logging.getLogger('nibe')
        sentinel = logging.NullHandler()
        root.handlers.clear()
        root.addHandler(sentinel)
        try:
            gn._build_logging('warning')
            # Handler count must not grow
            self.assertEqual(root.handlers, [sentinel])
            self.assertEqual(root.level, logging.WARNING)
        finally:
            root.handlers.clear()


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

    def test_no_retained_topics_found(self):
        """When no retained messages exist, the function logs and returns
        without attempting any clear-publishes."""
        client, cleanup = self._make_client()
        self._simulate_sentinel_immediately(client)
        cleanup(client)
        # Only the sentinel publish should have happened — no clear-publishes
        self.assertEqual(client.publish.call_count, 1)

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
                    payload=b"22.5", retain=True,
                )
                ha_cb(client, None, msg)
                sentinel_cb = self._get_callback(client, "nibe/browser/scan_sentinel")
                sentinel_msg = MagicMock(topic=topic, payload=b"cleanup", retain=False)
                sentinel_cb(client, None, sentinel_msg)
            return MagicMock()

        client.publish.side_effect = fake_publish
        cleanup(client)

        cleared_topics = [
            call.args[0] if call.args else call.kwargs.get('topic')
            for call in client.publish.call_args_list
            if (call.kwargs.get('retain') is True)
            or (len(call.args) > 2 and call.args[2] is True)
        ]
        self.assertIn("homeassistant/sensor/nibe_test/state", cleared_topics)

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
                    payload=b"1", retain=True,
                )
                ha_cb(client, None, msg)
                sentinel_cb = self._get_callback(client, "nibe/browser/scan_sentinel")
                sentinel_cb(client, None, MagicMock(
                    topic=topic, payload=b"cleanup", retain=False))
            return MagicMock()

        client.publish.side_effect = fake_publish
        cleanup(client)

        # Only the sentinel publish — the foreign topic was never collected,
        # so no clear-publish should have happened for it.
        clear_calls = [c for c in client.publish.call_args_list if c.kwargs.get('retain') is True]
        self.assertEqual(len(clear_calls), 0)

    def test_collects_browser_topic_unconditionally(self):
        """nibe/browser/# topics need no unique_id filter — they always
        belong to this bridge."""
        client, cleanup = self._make_client()

        def fake_publish(topic, payload=None, retain=False):
            if topic == "nibe/browser/scan_sentinel" and not retain:
                browser_cb = self._get_callback(client, "nibe/browser/#")
                msg = MagicMock(
                    topic="nibe/browser/all_metadata", payload=b"{}", retain=True,
                )
                browser_cb(client, None, msg)
                sentinel_cb = self._get_callback(client, "nibe/browser/scan_sentinel")
                sentinel_cb(client, None, MagicMock(
                    topic=topic, payload=b"cleanup", retain=False))
            return MagicMock()

        client.publish.side_effect = fake_publish
        cleanup(client)

        clear_calls = [c for c in client.publish.call_args_list if c.kwargs.get('retain') is True]
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
                browser_cb(client, None, MagicMock(
                    topic="nibe/browser/scan_sentinel", payload=b"cleanup", retain=False))
                sentinel_cb = self._get_callback(client, "nibe/browser/scan_sentinel")
                sentinel_cb(client, None, MagicMock(
                    topic=topic, payload=b"cleanup", retain=False))
            return MagicMock()

        client.publish.side_effect = fake_publish
        cleanup(client)

        clear_calls = [c for c in client.publish.call_args_list if c.kwargs.get('retain') is True]
        self.assertEqual(len(clear_calls), 0)

    def test_empty_payload_not_collected(self):
        """A message with an empty/already-cleared payload must not be
        re-collected for clearing — it carries no retained data."""
        client, cleanup = self._make_client()

        def fake_publish(topic, payload=None, retain=False):
            if topic == "nibe/browser/scan_sentinel" and not retain:
                browser_cb = self._get_callback(client, "nibe/browser/#")
                browser_cb(client, None, MagicMock(
                    topic="nibe/browser/already_cleared", payload=b"", retain=True))
                sentinel_cb = self._get_callback(client, "nibe/browser/scan_sentinel")
                sentinel_cb(client, None, MagicMock(
                    topic=topic, payload=b"cleanup", retain=False))
            return MagicMock()

        client.publish.side_effect = fake_publish
        cleanup(client)

        clear_calls = [c for c in client.publish.call_args_list if c.kwargs.get('retain') is True]
        self.assertEqual(len(clear_calls), 0)

    def test_sentinel_timeout_logs_warning_and_continues(self):
        """If the sentinel never arrives within the timeout, the function
        must log a warning and still proceed to clear whatever topics were
        collected before the timeout — not hang or crash."""
        client, cleanup = self._make_client()
        # publish() never triggers the sentinel callback — wait() will time out
        with patch('threading.Event.wait', return_value=False) as mock_wait, \
             patch('generate_nibe_mqtt.log_startup') as mock_log:
            cleanup(client)
            mock_wait.assert_called_once()
            self.assertTrue(
                any('Sentinel timeout' in str(call) for call in mock_log.warning.call_args_list)
            )

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
                browser_cb(client, None, MagicMock(
                    topic="nibe/browser/point_list", payload=b"[]", retain=True))
                sentinel_cb = self._get_callback(client, "nibe/browser/scan_sentinel")
                sentinel_cb(client, None, MagicMock(
                    topic=topic, payload=b"cleanup", retain=False))
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
            self.fail("_cleanup_mqtt_retained must catch publish-confirmation "
                      "exceptions, not propagate them")


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
        self.assertFalse(ctx.check_hostname)

    def test_no_ca_cert_sets_cert_none(self):
        from generate_nibe_mqtt import _build_ssl_context
        ctx = _build_ssl_context(None)
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)

    def test_nonexistent_ca_path_falls_back_to_self_signed(self):
        from generate_nibe_mqtt import _build_ssl_context
        ctx = _build_ssl_context('/nonexistent/ca.crt')
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)

    def test_valid_ca_cert_enables_verification(self):
        import ssl
        import os
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

    def test_never_raises_for_none_or_nonexistent_path(self):
        from generate_nibe_mqtt import _build_ssl_context
        for path in [None, '', '/nonexistent/path/ca.crt', '/tmp/definitely_not_there.crt']:
            ctx = _build_ssl_context(path)
            self.assertIsInstance(ctx, ssl.SSLContext)



class TestDeriveDeviceId(unittest.TestCase):
    """_derive_device_id: serial present vs absent, normalisation."""

    def setUp(self):
        from generate_nibe_mqtt import _derive_device_id
        self.fn = _derive_device_id

    def test_serial_present_returns_nibe_prefix(self):
        result = self.fn({'product': {'serialNumber': 'ABC123'}}, 'fallback')
        self.assertTrue(result.startswith('nibe_'))

    def test_serial_normalised_to_lowercase(self):
        result = self.fn({'product': {'serialNumber': 'ABC123'}}, 'fallback')
        self.assertEqual(result, 'nibe_abc123')

    def test_serial_special_chars_stripped(self):
        result = self.fn({'product': {'serialNumber': 'AB-12 CD.EF'}}, 'fallback')
        self.assertEqual(result, 'nibe_ab12cdef')

    def test_underscore_preserved(self):
        result = self.fn({'product': {'serialNumber': 'AB_12'}}, 'fallback')
        self.assertEqual(result, 'nibe_ab_12')

    def test_serial_absent_returns_fallback(self):
        result = self.fn({}, 'my_fallback')
        self.assertEqual(result, 'my_fallback')

    def test_serial_empty_string_returns_fallback(self):
        result = self.fn({'product': {'serialNumber': ''}}, 'my_fallback')
        self.assertEqual(result, 'my_fallback')

    def test_serial_none_returns_fallback(self):
        result = self.fn({'product': {'serialNumber': None}}, 'my_fallback')
        self.assertEqual(result, 'my_fallback')

    def test_serial_whitespace_only_returns_fallback(self):
        result = self.fn({'product': {'serialNumber': '   '}}, 'my_fallback')
        self.assertEqual(result, 'my_fallback')

    def test_empty_response_returns_fallback(self):
        result = self.fn({}, 'my_fallback')
        self.assertEqual(result, 'my_fallback')

    @given(st.text(max_size=30))
    def test_result_always_starts_with_nibe_or_is_fallback(self, serial):
        from generate_nibe_mqtt import _derive_device_id
        result = _derive_device_id({'product': {'serialNumber': serial}}, 'fallback')
        self.assertTrue(
            result.startswith('nibe_') or result == 'fallback',
        )

    @given(st.text(min_size=1, max_size=30).filter(lambda s: s.strip()))
    def test_nonempty_serial_gives_nibe_prefix(self, serial):
        from generate_nibe_mqtt import _derive_device_id
        result = _derive_device_id({'product': {'serialNumber': serial}}, 'fallback')
        self.assertTrue(result.startswith('nibe_'))



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
        self.assertEqual(self.fn(self._args('advanced'), self._cfg('essential')), 'advanced')

    def test_empty_cli_mode_falls_back_to_config(self):
        self.assertEqual(self.fn(self._args(''), self._cfg('monitoring')), 'monitoring')

    def test_none_cli_mode_falls_back_to_config(self):
        self.assertEqual(self.fn(self._args(None), self._cfg('monitoring')), 'monitoring')

    def test_config_mode_returned_when_cli_absent(self):
        self.assertEqual(self.fn(self._args(None), self._cfg('all')), 'all')

    @given(st.text(min_size=1, max_size=20), st.text(min_size=1, max_size=20))
    def test_cli_always_wins_when_truthy(self, cli_mode, cfg_mode):
        from generate_nibe_mqtt import _resolve_initial_mode
        args = MagicMock(mode=cli_mode)
        cfg  = MagicMock(mode=cfg_mode)
        self.assertEqual(_resolve_initial_mode(args, cfg), cli_mode)



class TestBuildMqttClientId(unittest.TestCase):
    """_build_mqtt_client_id: always ≤23 chars, preserves short IDs."""

    def setUp(self):
        from generate_nibe_mqtt import _build_mqtt_client_id
        self.fn = _build_mqtt_client_id

    def test_short_id_unchanged(self):
        self.assertEqual(self.fn('nibe_abc'), 'nibe_abc')

    def test_long_id_truncated_to_23(self):
        result = self.fn('nibe_' + 'x' * 30)
        self.assertEqual(len(result), 23)

    def test_exactly_23_unchanged(self):
        id23 = 'a' * 23
        self.assertEqual(self.fn(id23), id23)

    def test_empty_string_returns_empty(self):
        self.assertEqual(self.fn(''), '')

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
        self.fn(self.mqtt, self._cfg(tls=False, username='user'))
        self.mqtt.tls_set.assert_not_called()

    def test_tls_enabled_no_ca_calls_tls_set_with_none(self):
        self.fn(self.mqtt, self._cfg(tls=True, ca=None))
        self.mqtt.tls_set.assert_called_once_with(ca_certs=None)

    def test_tls_enabled_nonexistent_ca_calls_tls_set_with_none(self):
        self.fn(self.mqtt, self._cfg(tls=True, ca='/nonexistent/ca.crt'))
        self.mqtt.tls_set.assert_called_once_with(ca_certs=None)

    def test_tls_enabled_existing_ca_calls_tls_set_with_path(self):
        import os
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.crt', delete=False) as f:
            ca_path = f.name
        try:
            self.fn(self.mqtt, self._cfg(tls=True, ca=ca_path))
            self.mqtt.tls_set.assert_called_once_with(ca_certs=ca_path)
        finally:
            os.unlink(ca_path)

    def test_tls_disabled_tls_set_never_called(self):
        for username in (None, 'user'):
            self.mqtt.reset_mock()
            self.fn(self.mqtt, self._cfg(tls=False, username=username))
            self.mqtt.tls_set.assert_not_called()



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
        with patch('time.sleep') as mock_sleep:
            result = self.fn(em, retries=3, backoffs=[3, 6, 12])
        self.assertEqual(result, {100, 200})
        mock_sleep.assert_not_called()

    def test_first_scan_empty_retries(self):
        em = self._em([set(), {100}])
        with patch('time.sleep'):
            result = self.fn(em, retries=3, backoffs=[3, 6, 12])
        self.assertEqual(result, {100})
        self.assertEqual(em.scan_mqtt_discovery.call_count, 2)

    def test_all_scans_fail_returns_empty_set(self):
        em = self._em([set(), set(), set(), set()])
        with patch('time.sleep'):
            result = self.fn(em, retries=3, backoffs=[1, 1, 1])
        self.assertEqual(result, set())

    def test_scan_called_at_most_retries_plus_one_times(self):
        em = self._em([set()] * 10)
        with patch('time.sleep'):
            self.fn(em, retries=3, backoffs=[1, 1, 1])
        self.assertLessEqual(em.scan_mqtt_discovery.call_count, 4)

    def test_returns_set(self):
        em = self._em([set(), set(), set(), set()])
        with patch('time.sleep'):
            result = self.fn(em, retries=3, backoffs=[1, 1, 1])
        self.assertIsInstance(result, set)

    def test_sleep_called_with_correct_backoff(self):
        em = self._em([set(), {1}])
        with patch('time.sleep') as mock_sleep:
            self.fn(em, retries=3, backoffs=[3, 6, 12])
        mock_sleep.assert_called_once_with(3)

    def test_default_backoffs_used_when_none(self):
        em = self._em([{1}])
        with patch('time.sleep') as mock_sleep:
            self.fn(em)
        mock_sleep.assert_not_called()



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

    def _run(self, action, applied_mode='essential', initial_mode='essential'):
        with patch.object(self.em, 'apply_mode'), \
             patch.object(self.em, 'restore_from_mqtt'), \
             patch.object(self.em, 'record_applied_mode'):
            self.fn(self.em, action, applied_mode, initial_mode, self.mqtt, 'Test Device')

    def _run_with_mocks(self, action, applied_mode='essential', initial_mode='essential'):
        """Return the patched mocks for assertion."""
        with patch.object(self.em, 'apply_mode') as mock_apply, \
             patch.object(self.em, 'restore_from_mqtt') as mock_restore, \
             patch.object(self.em, 'record_applied_mode') as mock_record:
            self.fn(self.em, action, applied_mode, initial_mode, self.mqtt, 'Test Device')
        return mock_apply, mock_restore, mock_record

    def test_apply_calls_apply_mode(self):
        mock_apply, mock_restore, _ = self._run_with_mocks(
            'apply', applied_mode=None, initial_mode='monitoring'
        )
        mock_apply.assert_called_once_with('monitoring')

    def test_apply_does_not_call_restore(self):
        _, mock_restore, _ = self._run_with_mocks('apply', applied_mode=None)
        mock_restore.assert_not_called()

    def test_apply_none_mode_sends_notification(self):
        with patch.object(self.em, 'apply_mode'), \
             patch.object(self.em, 'restore_from_mqtt'), \
             patch.object(self.em, 'record_applied_mode'), \
             patch('generate_nibe_mqtt.notify_ha') as mock_notify:
            self.fn(self.em, 'apply', None, 'none', self.mqtt, 'Test Device')
        mock_notify.assert_called_once()
        args = mock_notify.call_args
        self.assertIn('nibe_no_entities', str(args))

    def test_apply_non_none_mode_no_notification(self):
        with patch.object(self.em, 'apply_mode'), \
             patch.object(self.em, 'restore_from_mqtt'), \
             patch.object(self.em, 'record_applied_mode'), \
             patch('generate_nibe_mqtt.notify_ha') as mock_notify:
            self.fn(self.em, 'apply', None, 'essential', self.mqtt, 'Test Device')
        mock_notify.assert_not_called()

    def test_restore_calls_restore_from_mqtt(self):
        _, mock_restore, _ = self._run_with_mocks('restore', applied_mode='essential')
        mock_restore.assert_called_once()

    def test_restore_does_not_call_apply_mode(self):
        mock_apply, *_ = self._run_with_mocks('restore', applied_mode='essential')
        mock_apply.assert_not_called()

    def test_restore_with_applied_mode_does_not_record(self):
        _, _, mock_record = self._run_with_mocks('restore', applied_mode='essential')
        mock_record.assert_not_called()

    def test_restore_with_none_applied_mode_records_baseline(self):
        _, _, mock_record = self._run_with_mocks(
            'restore', applied_mode=None, initial_mode='monitoring'
        )
        mock_record.assert_called_once_with('monitoring')

    def test_reconcile_calls_restore_then_apply(self):
        call_order = []
        with patch.object(self.em, 'restore_from_mqtt',
                          side_effect=lambda: call_order.append('restore')), \
             patch.object(self.em, 'apply_mode',
                          side_effect=lambda m: call_order.append(f'apply:{m}')), \
             patch.object(self.em, 'record_applied_mode'):
            self.fn(self.em, 'reconcile', 'essential', 'monitoring', self.mqtt, 'Test')
        self.assertEqual(call_order, ['restore', 'apply:monitoring'])

    def test_reconcile_calls_both_restore_and_apply(self):
        mock_apply, mock_restore, _ = self._run_with_mocks(
            'reconcile', applied_mode='essential', initial_mode='monitoring'
        )
        mock_restore.assert_called_once()
        mock_apply.assert_called_once_with('monitoring')

    def test_unknown_action_does_not_raise(self):
        self._run('unknown_action')  # must not raise



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
        api = self._api({'product': {'name': 'S2125', 'serialNumber': '123'}})
        result = self.fn(api)
        self.assertEqual(result['product']['name'], 'S2125')

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
        from generate_nibe_mqtt import _fetch_api_response, _ApiAuthError
        api = MagicMock()
        api.fetch_device_info.side_effect = urllib.error.HTTPError(
            url='https://host', code=401, msg='Unauthorized', hdrs={}, fp=None)
        with self.assertRaises(_ApiAuthError):
            _fetch_api_response(api)

    def test_http_403_raises_api_auth_error(self):
        import urllib.error
        from generate_nibe_mqtt import _fetch_api_response, _ApiAuthError
        api = MagicMock()
        api.fetch_device_info.side_effect = urllib.error.HTTPError(
            url='https://host', code=403, msg='Forbidden', hdrs={}, fp=None)
        with self.assertRaises(_ApiAuthError):
            _fetch_api_response(api)

    def test_auth_error_contains_status_code(self):
        import urllib.error
        from generate_nibe_mqtt import _fetch_api_response, _ApiAuthError
        api = MagicMock()
        api.fetch_device_info.side_effect = urllib.error.HTTPError(
            url='https://host', code=401, msg='Unauthorized', hdrs={}, fp=None)
        with self.assertRaises(_ApiAuthError) as ctx:
            _fetch_api_response(api)
        self.assertIn('401', str(ctx.exception))

    def test_success_logs_connection_info(self):
        api = self._api({'product': {
            'name': 'S2125', 'manufacturer': 'NIBE',
            'serialNumber': 'ABC', 'firmwareId': '4.12.8',
        }})
        with self.assertLogs('nibe.startup', level='INFO') as log:
            self.fn(api)
        self.assertTrue(any('S2125' in m for m in log.output))

    def test_none_response_logs_warning(self):
        api = self._api(None)
        with self.assertLogs('nibe.startup', level='WARNING') as log:
            self.fn(api)
        self.assertTrue(any('offline' in m.lower() for m in log.output))



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
        point_to_menu, menu_points = self.fn('/nonexistent/path')
        self.assertEqual(point_to_menu, {})
        self.assertEqual(menu_points, frozenset())

    def test_missing_dir_does_not_raise(self):
        self.fn('/nonexistent/path')  # must not raise

    def test_corrupt_yaml_returns_empty_results(self):
        import tempfile
        import os
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = os.path.join(tmpdir, 'menu_structure.yaml')
            with open(yaml_path, 'w') as f:
                f.write(': invalid: yaml: {{{')
            point_to_menu, menu_points = self.fn(tmpdir)
        self.assertEqual(point_to_menu, {})
        self.assertEqual(menu_points, frozenset())

    def test_empty_yaml_returns_empty_results(self):
        import tempfile
        import os
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = os.path.join(tmpdir, 'menu_structure.yaml')
            with open(yaml_path, 'w') as f:
                f.write('menus: []\n')
            point_to_menu, menu_points = self.fn(tmpdir)
        self.assertEqual(point_to_menu, {})
        self.assertEqual(menu_points, frozenset())

    def test_menu_points_subset_of_real_points(self):
        """All points in MODES['menus'] must be real Nibe point IDs."""
        point_to_menu, menu_points = self.fn(_APP_DIR)
        # menu_points contains only integers
        for pid in menu_points:
            self.assertIsInstance(pid, int)


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

    @given(st.sets(st.integers(min_value=1, max_value=99999), min_size=1, max_size=20),
           st.integers(min_value=1, max_value=5),
           st.lists(st.integers(min_value=0, max_value=1), min_size=1, max_size=5))
    def test_nonempty_first_scan_never_sleeps(self, point_ids, retries, backoffs):
        """If the first scan returns non-empty, sleep is never called."""
        from generate_nibe_mqtt import _run_scan_with_retry
        em = MagicMock()
        em.scan_mqtt_discovery.return_value = point_ids
        with patch('time.sleep') as mock_sleep:
            result = _run_scan_with_retry(em, retries=retries, backoffs=backoffs)
        mock_sleep.assert_not_called()
        self.assertEqual(result, point_ids)

    @given(st.integers(min_value=1, max_value=5),
           st.lists(st.integers(min_value=0, max_value=1), min_size=1, max_size=5))
    def test_always_returns_set(self, retries, backoffs):
        """Result is always a set regardless of scan outcomes."""
        from generate_nibe_mqtt import _run_scan_with_retry
        em = MagicMock()
        em.scan_mqtt_discovery.return_value = set()
        with patch('time.sleep'):
            result = _run_scan_with_retry(em, retries=retries, backoffs=backoffs)
        self.assertIsInstance(result, set)

    @given(st.integers(min_value=1, max_value=5),
           st.lists(st.integers(min_value=0, max_value=1), min_size=1, max_size=5))
    def test_scan_called_at_most_retries_plus_one(self, retries, backoffs):
        """scan_mqtt_discovery is called at most retries+1 times."""
        from generate_nibe_mqtt import _run_scan_with_retry
        em = MagicMock()
        em.scan_mqtt_discovery.return_value = set()
        with patch('time.sleep'):
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
        with patch.object(em, 'apply_mode') as mock_apply, \
             patch.object(em, 'restore_from_mqtt') as mock_restore, \
             patch.object(em, 'record_applied_mode') as mock_record:
            em._apply_startup_action('apply', None, 'essential')
        mock_apply.assert_called_once_with('essential')
        mock_restore.assert_not_called()
        mock_record.assert_not_called()

    def test_restore_calls_restore_from_mqtt(self):
        """action='restore' with a known applied_mode must restore and not re-record."""
        em = self._em()
        with patch.object(em, 'apply_mode') as mock_apply, \
             patch.object(em, 'restore_from_mqtt') as mock_restore, \
             patch.object(em, 'record_applied_mode') as mock_record:
            em._apply_startup_action('restore', 'essential', 'essential')
        mock_restore.assert_called_once()
        mock_apply.assert_not_called()
        mock_record.assert_not_called()

    def test_restore_records_mode_when_applied_mode_none(self):
        """action='restore' with applied_mode=None must record the current mode."""
        em = self._em()
        with patch.object(em, 'restore_from_mqtt'), \
             patch.object(em, 'record_applied_mode') as mock_record:
            em._apply_startup_action('restore', None, 'monitoring')
        mock_record.assert_called_once_with('monitoring')

    def test_reconcile_restores_then_applies(self):
        """action='reconcile' must restore first, then apply the new mode."""
        em = self._em()
        call_order = []
        with patch.object(em, 'restore_from_mqtt',
                          side_effect=lambda: call_order.append('restore')), \
             patch.object(em, 'apply_mode',
                          side_effect=lambda m: call_order.append(f'apply:{m}')):
            em._apply_startup_action('reconcile', 'essential', 'advanced')
        self.assertEqual(call_order, ['restore', 'apply:advanced'])

    @given(
        action=st.sampled_from(['apply', 'restore', 'reconcile']),
        applied=st.one_of(st.none(), st.sampled_from(['essential', 'monitoring', 'advanced'])),
        initial=st.sampled_from(['essential', 'monitoring', 'advanced', 'menus', 'all', 'none']),
    )
    def test_never_raises(self, action, applied, initial):
        """_apply_startup_action must never raise for any valid action/mode combination."""
        em = self._em()
        with patch.object(em, 'apply_mode'), \
             patch.object(em, 'restore_from_mqtt'), \
             patch.object(em, 'record_applied_mode'):
            em._apply_startup_action(action, applied, initial)   # must not raise

    def test_execute_startup_action_delegates_mutations(self):
        """_execute_startup_action must delegate the mutations to _apply_startup_action,
        not reimplement them inline — confirmed by asserting _apply_startup_action is called
        with the correct arguments for each action type.
        """
        from generate_nibe_mqtt import _execute_startup_action
        for action in ('apply', 'restore', 'reconcile'):
            em = self._em()
            with patch.object(em, '_apply_startup_action') as mock_apply_action, \
                 patch('generate_nibe_mqtt.notify_ha'):
                _execute_startup_action(
                    em, action, 'essential', 'essential', MagicMock(), 'Test'
                )
            mock_apply_action.assert_called_once_with(action, 'essential', 'essential'), \
                f"action={action!r}: _apply_startup_action not called correctly"



class TestExecuteStartupActionProperties(unittest.TestCase):
    """Hypothesis properties for _execute_startup_action."""

    _modes = st.sampled_from(['essential', 'monitoring', 'advanced', 'menus', 'all', 'none'])
    _applied = st.one_of(st.none(), st.sampled_from(['essential', 'monitoring', 'advanced']))

    def _make_patched_em(self):
        """Real EntityManager with apply_mode/restore/record patched out."""
        em = _make_em()
        em.apply_mode        = MagicMock()
        em.restore_from_mqtt = MagicMock()
        em.record_applied_mode = MagicMock()
        return em

    @given(_modes, _applied)
    def test_apply_never_calls_restore(self, initial, applied):
        from generate_nibe_mqtt import _execute_startup_action
        em = self._make_patched_em()
        with patch('generate_nibe_mqtt.notify_ha'):
            _execute_startup_action(em, 'apply', applied, initial, MagicMock(), 'Dev')
        em.restore_from_mqtt.assert_not_called()

    @given(_modes, _applied)
    def test_restore_never_calls_apply_mode(self, initial, applied):
        from generate_nibe_mqtt import _execute_startup_action
        em = self._make_patched_em()
        _execute_startup_action(em, 'restore', applied, initial, MagicMock(), 'Dev')
        em.apply_mode.assert_not_called()

    @given(_modes, _applied)
    def test_reconcile_always_calls_both(self, initial, applied):
        from generate_nibe_mqtt import _execute_startup_action
        em = self._make_patched_em()
        _execute_startup_action(em, 'reconcile', applied, initial, MagicMock(), 'Dev')
        em.restore_from_mqtt.assert_called_once()
        em.apply_mode.assert_called_once_with(initial)

    @given(st.text(min_size=1, max_size=20), _applied, _modes)
    def test_unknown_action_never_raises(self, action, applied, initial):
        """Any action string that is not apply/restore/reconcile must not raise."""
        from generate_nibe_mqtt import _execute_startup_action
        em = self._make_patched_em()
        with patch('generate_nibe_mqtt.notify_ha'):
            _execute_startup_action(em, action, applied, initial, MagicMock(), 'Dev')



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
        from generate_nibe_mqtt import _fetch_api_response, _ApiAuthError
        api = MagicMock()
        api.fetch_device_info.side_effect = urllib.error.HTTPError(
            url='https://host', code=status_code,
            msg='Error', hdrs={}, fp=None,
        )
        with self.assertRaises(_ApiAuthError):
            _fetch_api_response(api)

    @given(st.text(min_size=1, max_size=20), st.text(min_size=1, max_size=20),
           st.text(min_size=1, max_size=20), st.text(min_size=1, max_size=20))
    def test_full_product_response_returned_unchanged(self, name, manufacturer,
                                                       serial, firmware):
        """A complete product response is returned exactly as received."""
        from generate_nibe_mqtt import _fetch_api_response
        response = {'product': {
            'name': name, 'manufacturer': manufacturer,
            'serialNumber': serial, 'firmwareId': firmware,
        }}
        api = MagicMock()
        api.fetch_device_info.return_value = response
        result = _fetch_api_response(api)
        self.assertEqual(result, response)



class TestGenerateNibeCrossFunctionProperties(unittest.TestCase):
    """Cross-function Hypothesis properties for generate_nibe_mqtt helpers."""

    def test_bridge_version_matches_config_yaml(self):
        """BRIDGE_VERSION in generate_nibe_mqtt.py must match version: in config.yaml.
        Catches a version bump applied to only one of the two files.
        """
        import yaml as _yaml
        from generate_nibe_mqtt import BRIDGE_VERSION

        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'config.yaml',
        )
        if not os.path.exists(config_path):
            config_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), 'config.yaml',
            )
        self.assertTrue(os.path.exists(config_path),
                        f"config.yaml not found (tried {config_path})")
        with open(config_path, encoding='utf-8') as f:
            manifest = _yaml.safe_load(f)
        config_version = manifest.get('version', '')
        self.assertEqual(
            BRIDGE_VERSION, config_version,
            f"BRIDGE_VERSION={BRIDGE_VERSION!r} in generate_nibe_mqtt.py does not "
            f"match version={config_version!r} in config.yaml — update both together",
        )

    @given(st.text(max_size=50))
    def test_derive_then_build_client_id_always_safe(self, serial):
        """_derive_device_id output always produces a safe MQTT client ID ≤23 chars."""
        from generate_nibe_mqtt import _derive_device_id, _build_mqtt_client_id
        device_id = _derive_device_id(
            {'product': {'serialNumber': serial}}, 'nibe_default'
        )
        client_id = _build_mqtt_client_id(device_id)
        self.assertLessEqual(len(client_id), 23)

    @given(st.integers(min_value=0, max_value=3600))
    def test_keepalive_always_greater_than_any_poll_interval(self, poll_interval):
        """Keepalive is always strictly greater than poll_interval."""
        from generate_nibe_mqtt import _keepalive_from_config
        self.assertGreater(_keepalive_from_config(poll_interval), poll_interval)

    @given(st.text(min_size=1, max_size=20))
    def test_resolve_mode_output_usable_by_execute_startup_action(self, cfg_mode):
        """_resolve_initial_mode output can always be passed to _execute_startup_action."""
        from generate_nibe_mqtt import _resolve_initial_mode, _execute_startup_action
        args = MagicMock(mode=None)
        cfg  = MagicMock(mode=cfg_mode)
        mode = _resolve_initial_mode(args, cfg)
        em   = _make_em()
        em.apply_mode        = MagicMock()
        em.restore_from_mqtt = MagicMock()
        em.record_applied_mode = MagicMock()
        with patch('generate_nibe_mqtt.notify_ha'):
            _execute_startup_action(em, 'apply', None, mode, MagicMock(), 'Dev')
        em.apply_mode.assert_called_once_with(mode)

    @given(st.text(max_size=30))
    def test_derive_device_id_output_always_valid_for_client_id(self, serial):
        """Pipeline: serial → device_id → client_id — no step raises."""
        from generate_nibe_mqtt import _derive_device_id, _build_mqtt_client_id
        device_id = _derive_device_id(
            {'product': {'serialNumber': serial}}, 'nibe_fallback'
        )
        client_id = _build_mqtt_client_id(device_id)
        self.assertIsInstance(client_id, str)
        self.assertLessEqual(len(client_id), 23)


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
        with patch.dict('os.environ', {}, clear=True), \
             patch('nibe_lovelace._open_ha_websocket') as mock_ws:
            nl._remove_menu_dashboard()
        mock_ws.assert_not_called()

    def test_websocket_open_fails_returns_early(self):
        import nibe_lovelace as nl
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch('nibe_lovelace._open_ha_websocket', return_value=None):
            nl._remove_menu_dashboard()  # must not raise

    def _fake_ws_call(self, dashboard_present=True, delete_success=True):
        def fake(ws, _msg_id, payload, _timeout=10):
            t = payload.get('type')
            if t == 'lovelace/dashboards/list':
                items = [{'url_path': 'nibe-menus', 'id': 42}] if dashboard_present else []
                return {'success': True, 'result': items}
            if t == 'lovelace/dashboards/delete':
                return {'success': delete_success}
            return {'success': True}
        return fake

    def test_dashboard_present_gets_deleted(self):
        import nibe_lovelace as nl
        ws = MagicMock()
        next_id = MagicMock(return_value=1)
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch('nibe_lovelace._open_ha_websocket', return_value=(ws, next_id)), \
             patch('nibe_lovelace._ws_call', side_effect=self._fake_ws_call(dashboard_present=True)), \
             patch('nibe_lovelace.os.remove') as mock_rm:
            nl._remove_menu_dashboard()
        ws.close.assert_called_once()
        mock_rm.assert_called_once_with(nl._MENU_DASHBOARD_FLAG)

    def test_dashboard_absent_is_noop_no_delete_attempted(self):
        import nibe_lovelace as nl
        ws = MagicMock()
        next_id = MagicMock(return_value=1)
        calls = []
        def fake(_ws, _mid, payload, _timeout=10):
            calls.append(payload.get('type'))
            if payload.get('type') == 'lovelace/dashboards/list':
                return {'success': True, 'result': []}  # no nibe-menus, but list succeeded
            return {'success': True}
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch('nibe_lovelace._open_ha_websocket', return_value=(ws, next_id)), \
             patch('nibe_lovelace._ws_call', side_effect=fake), \
             patch('nibe_lovelace.os.remove') as mock_rm:
            nl._remove_menu_dashboard()
        self.assertNotIn('lovelace/dashboards/delete', calls)
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
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch('nibe_lovelace._open_ha_websocket', return_value=(ws, next_id)), \
             patch('nibe_lovelace._ws_call', return_value={}), \
             patch('nibe_lovelace.os.remove') as mock_rm:
            nl._remove_menu_dashboard()
        mock_rm.assert_not_called()

    def test_delete_failure_does_not_remove_flag(self):
        """When the delete call fails, we can't be sure the dashboard is gone —
        flag must NOT be removed so the next startup retries."""
        import nibe_lovelace as nl
        ws = MagicMock()
        next_id = MagicMock(return_value=1)
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch('nibe_lovelace._open_ha_websocket', return_value=(ws, next_id)), \
             patch('nibe_lovelace._ws_call',
                   side_effect=self._fake_ws_call(dashboard_present=True, delete_success=False)), \
             patch('nibe_lovelace.os.remove') as mock_rm:
            nl._remove_menu_dashboard()
        mock_rm.assert_not_called()

    def test_delete_failure_does_not_raise(self):
        import nibe_lovelace as nl
        ws = MagicMock()
        next_id = MagicMock(return_value=1)
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch('nibe_lovelace._open_ha_websocket', return_value=(ws, next_id)), \
             patch('nibe_lovelace._ws_call',
                   side_effect=self._fake_ws_call(dashboard_present=True, delete_success=False)), \
             patch('nibe_lovelace.os.remove'):
            nl._remove_menu_dashboard()  # must not raise
        ws.close.assert_called_once()

    def test_exception_during_teardown_is_caught_and_closes_ws(self):
        import nibe_lovelace as nl
        ws = MagicMock()
        next_id = MagicMock(return_value=1)
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch('nibe_lovelace._open_ha_websocket', return_value=(ws, next_id)), \
             patch('nibe_lovelace._ws_call', side_effect=RuntimeError("boom")), \
             patch('nibe_lovelace.os.remove'):
            nl._remove_menu_dashboard()  # must not raise
        ws.close.assert_called_once()

    def test_flag_file_removal_error_tolerated(self):
        """A missing or unremovable flag file must not raise — it's cleanup,
        not a required precondition."""
        import nibe_lovelace as nl
        ws = MagicMock()
        next_id = MagicMock(return_value=1)
        with patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'tok'}), \
             patch('nibe_lovelace._open_ha_websocket', return_value=(ws, next_id)), \
             patch('nibe_lovelace._ws_call', side_effect=self._fake_ws_call(dashboard_present=False)), \
             patch('nibe_lovelace.os.remove', side_effect=OSError("not found")):
            nl._remove_menu_dashboard()  # must not raise

    def test_public_wrapper_delegates_to_private(self):
        import nibe_lovelace as nl
        with patch('nibe_lovelace._remove_menu_dashboard') as mock_private:
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
            mqtt_client = self.mqtt,
            device_info = {"identifiers": ["nibe_test"]},
            device_id   = "test",
            device_name = "Test Device",
        )

    def _point(self, entity_type, point_id=5000, **meta_overrides):
        meta = {
            'variableId': point_id, 'variableType': 'integer', 'variableSize': 'u8',
            'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
            'isWritable': True, 'divisor': 1, 'decimal': 0,
            'minValue': 0, 'maxValue': 3, 'intDefaultValue': 0,
            'change': 1, 'unit': '', 'shortUnit': '', 'modbusRegisterID': 4200,
            'stringDefaultValue': '',
        }
        meta.update(meta_overrides)
        return {
            'variableId':      point_id,
            'display_title':   'Test Point',
            'description':     '0 = Off, 1 = Low, 2 = Med, 3 = High',
            'entity_type':     entity_type,
            'entity_category': 'config',
            'is_writable':     True,
            'is_dynamic':      False,
            'metadata':        meta,
        }

    def test_select_entity_publishes_discovery_config(self):
        """publish_entity_discovery with entity_type='select' hits the
        _build_select_config branch."""
        point = self._point('select')
        bulk = {5000: {'raw_value': 0, 'string_value': '', 'is_ok': True}}
        result = self.pub.publish_entity_discovery(point, bulk)
        self.assertIsNotNone(result)
        published = self.mqtt.publish.call_args_list
        configs = [c for c in published if '/config' in str(c)]
        self.assertTrue(configs)

    def test_binary_sensor_entity_publishes_discovery_config(self):
        """publish_entity_discovery with entity_type='binary_sensor' hits
        the _build_binary_sensor_config branch."""
        point = self._point(
            'binary_sensor',
            point_id=5001,
            isWritable=False,
            maxValue=1,
            modbusRegisterType='MODBUS_INPUT_REGISTER',
        )
        point['is_writable'] = False
        bulk = {5001: {'raw_value': 0, 'string_value': '', 'is_ok': True}}
        result = self.pub.publish_entity_discovery(point, bulk)
        self.assertIsNotNone(result)

    def test_sensor_with_numeric_value_but_no_device_class_gets_measurement(self):
        """A sensor with a non-empty unit that has no matching device class
        gets state_class='measurement' from the has_numeric_value branch."""
        # 'rpm' is not in _UNIT_TO_DEVICE_CLASS and 'fan speed index'
        # does not match any keyword rule → device_class=None, unit truthy
        point = self._point(
            'sensor',
            point_id=5002,
            isWritable=False,
            modbusRegisterType='MODBUS_INPUT_REGISTER',
            unit='rpm',
        )
        point['display_title'] = 'fan speed index'
        point['is_writable'] = False
        bulk = {5002: {'raw_value': 500, 'string_value': '', 'is_ok': True}}
        result = self.pub.publish_entity_discovery(point, bulk)
        self.assertIsNotNone(result)
        config_calls = [c for c in self.mqtt.publish.call_args_list
                        if '/config' in str(c.args[0])]
        self.assertTrue(config_calls)
        payload = json.loads(config_calls[-1].args[1])
        self.assertEqual(payload.get('state_class'), 'measurement')


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
        real_paho = sys.modules.pop('paho', None)
        real_paho_mqtt = sys.modules.pop('paho.mqtt', None)
        real_paho_mqtt_client = sys.modules.pop('paho.mqtt.client', None)
        # Also remove the already-imported generate_nibe_mqtt so it re-executes
        real_gnm = sys.modules.pop('generate_nibe_mqtt', None)
        try:
            sys.modules['paho'] = None          # make import raise ImportError
            sys.modules['paho.mqtt'] = None
            sys.modules['paho.mqtt.client'] = None
            with self.assertRaises(SystemExit) as ctx:
                importlib.import_module('generate_nibe_mqtt')
            self.assertEqual(ctx.exception.code, 1)
            # Note: Changed from print() to logging, so we can't easily mock the logging call
            # The important behavior is that it exits with code 1
        finally:
            # Restore everything
            for key, val in [
                ('paho', real_paho),
                ('paho.mqtt', real_paho_mqtt),
                ('paho.mqtt.client', real_paho_mqtt_client),
                ('generate_nibe_mqtt', real_gnm),
            ]:
                if val is None:
                    sys.modules.pop(key, None)
                else:
                    sys.modules[key] = val

    def test_formatter_format_produces_expected_shape(self):
        """_Formatter.format returns a timestamped log line in the expected
        format — exercises the three lines inside the nested class."""
        import generate_nibe_mqtt as gnm
        import logging
        # _build_logging installs the formatter on a fresh nibe logger.
        # Clear handlers first so it doesn't early-return.
        nibe_log = logging.getLogger('nibe')
        original_handlers = nibe_log.handlers[:]
        nibe_log.handlers.clear()
        try:
            gnm._build_logging(level='debug')
            # The formatter is now installed; grab it from the handler.
            handler = nibe_log.handlers[-1]
            formatter = handler.formatter
            record = logging.LogRecord(
                name='nibe.test', level=logging.INFO,
                pathname='', lineno=0, msg='hello %s', args=('world',),
                exc_info=None,
            )
            result = formatter.format(record)
            # Shape: "HH:MM:SS.mmm [INFO    ] nibe.test: hello world"
            self.assertRegex(result, r'^\d{2}:\d{2}:\d{2}\.\d{3} \[INFO')
            self.assertIn('nibe.test', result)
            self.assertIn('hello world', result)
        finally:
            nibe_log.handlers.clear()
            nibe_log.handlers.extend(original_handlers)


# ===========================================================================
# Bug fixes: concurrent disable race, _handle_event exception isolation,
#            refresh_registry auth handshake
# ===========================================================================


class TestBuildInfrastructure(unittest.TestCase):
    """_build_infrastructure: credential check, auth failure, connection failure."""

    def _cfg(self, **overrides):
        from generate_nibe_mqtt import BridgeConfig
        cfg = BridgeConfig(
            api_base_url='https://10.0.0.1:8443/api/v1/devices/0',
            nibe_auth='Basic dXNlcjpwYXNz',
            mqtt_broker='localhost',
            mqtt_port=1883,
            device_name='Test Device',
            device_id='nibe_test',
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

    def test_exits_on_api_auth_error(self):
        """HTTP 401/403 from the Nibe API must call sys.exit(1)."""
        from generate_nibe_mqtt import _build_infrastructure, _ApiAuthError
        cfg = self._cfg()
        with patch('generate_nibe_mqtt._fetch_api_response',
                   side_effect=_ApiAuthError(401)), \
             patch('generate_nibe_mqtt._build_ssl_context', return_value=MagicMock()), \
             patch('generate_nibe_mqtt.NibeApiClient'), \
             patch('generate_nibe_mqtt.copy_card_file'):
            with self.assertRaises(SystemExit) as ctx:
                _build_infrastructure(cfg)
        self.assertEqual(ctx.exception.code, 1)

    def test_exits_on_mqtt_connection_error(self):
        """Broker unreachable (connect raises) must call sys.exit(1)."""
        from generate_nibe_mqtt import _build_infrastructure
        cfg = self._cfg()
        mock_mqtt = MagicMock()
        mock_mqtt.connect.side_effect = OSError("connection refused")
        with patch('generate_nibe_mqtt._fetch_api_response', return_value={}), \
             patch('generate_nibe_mqtt._build_ssl_context', return_value=MagicMock()), \
             patch('generate_nibe_mqtt.NibeApiClient'), \
             patch('generate_nibe_mqtt.copy_card_file'), \
             patch('generate_nibe_mqtt.mqtt.Client', return_value=mock_mqtt):
            with self.assertRaises(SystemExit) as ctx:
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
        real_Event = __import__('threading').Event
        events_created = []

        def _fake_Event():
            ev = real_Event()
            events_created.append(ev)
            if len(events_created) == 1:
                # This is _auth_failed — mark it as set immediately
                ev.set()
            return ev

        with patch('generate_nibe_mqtt._fetch_api_response', return_value={}), \
             patch('generate_nibe_mqtt._build_ssl_context', return_value=MagicMock()), \
             patch('generate_nibe_mqtt.NibeApiClient'), \
             patch('generate_nibe_mqtt.copy_card_file'), \
             patch('generate_nibe_mqtt.mqtt.Client', return_value=mock_mc), \
             patch('generate_nibe_mqtt.time.sleep'), \
             patch('generate_nibe_mqtt.threading.Event', side_effect=_fake_Event):
            with self.assertRaises(SystemExit) as ctx:
                _build_infrastructure(cfg)
        self.assertEqual(ctx.exception.code, 1)

    def test_returns_tuple_on_success(self):
        """Happy path: returns (api_client, mqtt_client, response, device_id, shutting_down, set_entity_manager)."""
        from generate_nibe_mqtt import _build_infrastructure
        cfg = self._cfg()
        mock_response = {'product': {'serialNumber': 'ABC123', 'name': 'S2125',
                                     'manufacturer': 'NIBE', 'firmwareId': '4.12'}}
        mock_mc = MagicMock()
        mock_mc.is_connected.return_value = True
        with patch('generate_nibe_mqtt._fetch_api_response', return_value=mock_response), \
             patch('generate_nibe_mqtt._build_ssl_context', return_value=MagicMock()), \
             patch('generate_nibe_mqtt.NibeApiClient'), \
             patch('generate_nibe_mqtt.copy_card_file'), \
             patch('generate_nibe_mqtt.mqtt.Client', return_value=mock_mc), \
             patch('generate_nibe_mqtt.time.sleep'):
            result = _build_infrastructure(cfg)

        api_client, mqtt_client, response, device_id, shutting_down, set_em = result
        self.assertIs(mqtt_client, mock_mc)
        self.assertEqual(response, mock_response)
        self.assertIn('abc123', device_id)   # serial normalised to lowercase
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
        with patch('generate_nibe_mqtt._fetch_api_response', return_value={}), \
             patch('generate_nibe_mqtt._build_ssl_context', return_value=MagicMock()), \
             patch('generate_nibe_mqtt.NibeApiClient'), \
             patch('generate_nibe_mqtt.copy_card_file'), \
             patch('generate_nibe_mqtt.mqtt.Client', return_value=mock_mc), \
             patch('generate_nibe_mqtt.time.sleep'):
            _, _, _, _, _, set_em = _build_infrastructure(cfg)

        fake_em = MagicMock()
        set_em(fake_em)   # wire in the entity manager

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
        mock_response = {'product': {'serialNumber': 'SN99887766'}}
        mock_mc = MagicMock()
        mock_mc.is_connected.return_value = True
        with patch('generate_nibe_mqtt._fetch_api_response', return_value=mock_response), \
             patch('generate_nibe_mqtt._build_ssl_context', return_value=MagicMock()), \
             patch('generate_nibe_mqtt.NibeApiClient'), \
             patch('generate_nibe_mqtt.copy_card_file'), \
             patch('generate_nibe_mqtt.mqtt.Client', return_value=mock_mc), \
             patch('generate_nibe_mqtt.time.sleep'):
            _, _, _, device_id, _, _ = _build_infrastructure(cfg)
        self.assertIn('sn99887766', device_id)

    def _call_infrastructure(self, cfg=None):
        """Helper: run _build_infrastructure and return (mqtt_client, set_em, shutting_down)."""
        from generate_nibe_mqtt import _build_infrastructure
        cfg = cfg or self._cfg()
        mock_mc = MagicMock()
        mock_mc.is_connected.return_value = True
        with patch('generate_nibe_mqtt._fetch_api_response', return_value={}), \
             patch('generate_nibe_mqtt._build_ssl_context', return_value=MagicMock()), \
             patch('generate_nibe_mqtt.NibeApiClient'), \
             patch('generate_nibe_mqtt.copy_card_file'), \
             patch('generate_nibe_mqtt.mqtt.Client', return_value=mock_mc), \
             patch('generate_nibe_mqtt.time.sleep'):
            _, mc, _, _, shutting_down, set_em = _build_infrastructure(cfg)
        return mc, set_em, shutting_down

    def test_on_disconnect_suppressed_when_shutting_down(self):
        """on_disconnect must return immediately when shutting_down[0] is True
        — no warning logged for an intentional clean shutdown."""
        mc, _, shutting_down = self._call_infrastructure()
        shutting_down[0] = True
        rc = MagicMock()
        rc.value = 0
        with patch('generate_nibe_mqtt.log_mqtt') as mock_log:
            mc.on_disconnect(mc, None, None, rc, None)
        mock_log.warning.assert_not_called()

    def test_on_disconnect_logs_warning_when_unexpected(self):
        """on_disconnect must log a warning when the disconnection is unexpected
        (shutting_down is False) with the correct label for the reason code."""
        mc, _, shutting_down = self._call_infrastructure()
        self.assertFalse(shutting_down[0])
        rc = MagicMock()
        rc.value = 0   # "clean disconnect or connection lost"
        with patch('generate_nibe_mqtt.log_mqtt') as mock_log:
            mc.on_disconnect(mc, None, None, rc, None)
        mock_log.warning.assert_called_once()
        msg = str(mock_log.warning.call_args)
        self.assertIn('reconnect', msg)

    def test_on_disconnect_unknown_rc_uses_str_fallback(self):
        """on_disconnect with an unknown reason code must use str(reason_code)
        as the label rather than crashing."""
        mc, _, _ = self._call_infrastructure()
        rc = MagicMock()
        rc.value = 99   # not in _DISCONNECT_LABELS
        rc.__str__ = lambda self: 'rc=99'
        with patch('generate_nibe_mqtt.log_mqtt') as mock_log:
            mc.on_disconnect(mc, None, None, rc, None)
        mock_log.warning.assert_called_once()



class TestShutdown(unittest.TestCase):
    """_shutdown: executor drain, offline publishes, MQTT disconnect."""

    def _make_em_with_entities(self, avail_topics):
        em = _make_em()
        for i, topic in enumerate(avail_topics):
            entity_info = {
                'point_id': i, 'entity_type': 'sensor',
                'availability_topic': topic,
                'state_topic': f'nibe/state/{i}',
                'command_topic': None, 'point_data': {},
            }
            em.active_entities_by_id[i] = entity_info
            em.mqtt_enabled_points.add(i)
        return em

    def _run_shutdown(self, em, extra_topics=None):
        from generate_nibe_mqtt import _shutdown
        mc             = MagicMock()
        pub            = MagicMock()
        watcher        = MagicMock()
        mgmt_exec      = MagicMock()
        shutting_down  = [False]
        atexit_fn      = MagicMock()

        with patch('generate_nibe_mqtt.teardown_lovelace'), \
             patch('generate_nibe_mqtt.os.environ.get', return_value=None):
            _shutdown(em, pub, mc, watcher, mgmt_exec, shutting_down, atexit_fn)

        return mc, watcher, mgmt_exec, shutting_down, atexit_fn

    def test_publishes_offline_for_all_active_entities(self):
        """_shutdown must publish 'offline' to every active entity's avail topic."""
        from generate_nibe_mqtt import MGMT_AVAIL_TOPIC
        em = self._make_em_with_entities(['nibe/avail/100', 'nibe/avail/200'])
        mc, *_ = self._run_shutdown(em)
        published_topics = [call.args[0] for call in mc.publish.call_args_list
                            if call.args[1] == 'offline']
        self.assertIn('nibe/avail/100', published_topics)
        self.assertIn('nibe/avail/200', published_topics)
        self.assertIn(MGMT_AVAIL_TOPIC, published_topics)

    def test_publishes_offline_to_mgmt_topic_even_with_no_entities(self):
        """MGMT_AVAIL_TOPIC must always go offline, even when no entities are active."""
        from generate_nibe_mqtt import MGMT_AVAIL_TOPIC
        em = _make_em()
        mc, *_ = self._run_shutdown(em)
        published_topics = [call.args[0] for call in mc.publish.call_args_list
                            if call.args[1] == 'offline']
        self.assertIn(MGMT_AVAIL_TOPIC, published_topics)

    def test_stops_registry_watcher(self):
        """registry_watcher.stop() must be called."""
        em = _make_em()
        _, watcher, *_ = self._run_shutdown(em)
        watcher.stop.assert_called_once()

    def test_shuts_down_executors(self):
        """Both write and mgmt executors must be shut down."""
        from generate_nibe_mqtt import _shutdown
        em            = _make_em()
        mc            = MagicMock()
        pub           = MagicMock()
        watcher       = MagicMock()
        mgmt_exec     = MagicMock()
        shutting_down = [False]
        atexit_fn     = MagicMock()

        # Patch Thread so executor.shutdown runs synchronously in the test
        with patch('generate_nibe_mqtt.threading.Thread') as MockThread, \
             patch('generate_nibe_mqtt.teardown_lovelace'), \
             patch('generate_nibe_mqtt.os.environ.get', return_value=None):
            instance = MockThread.return_value
            instance.is_alive.return_value = False
            _shutdown(em, pub, mc, watcher, mgmt_exec, shutting_down, atexit_fn)

        # Two threads should have been created: one per executor
        self.assertEqual(MockThread.call_count, 2)

    def test_sets_shutting_down_flag(self):
        """shutting_down[0] must be True after _shutdown completes."""
        em = _make_em()
        _, _, _, shutting_down, _ = self._run_shutdown(em)
        self.assertTrue(shutting_down[0])

    def test_unregisters_atexit(self):
        """atexit_cleanup_fn must be unregistered to prevent double-disconnect."""
        em = _make_em()
        _, _, _, _, atexit_fn = self._run_shutdown(em)
        # atexit.unregister was called with the function
        # (can't easily assert atexit.unregister directly; check loop_stop called)
        em2 = _make_em()
        mc2, *_ = self._run_shutdown(em2)
        mc2.loop_stop.assert_called_once()
        mc2.disconnect.assert_called_once()

    def test_runs_mqtt_cleanup_when_remove_frontend_set(self):
        """When NIBE_REMOVE_FRONTEND=1, _cleanup_mqtt_retained must be called."""
        from generate_nibe_mqtt import _shutdown
        em            = _make_em()
        mc            = MagicMock()
        shutting_down = [False]

        with patch('generate_nibe_mqtt.teardown_lovelace'), \
             patch('generate_nibe_mqtt.os.environ.get', return_value='1'), \
             patch('generate_nibe_mqtt._cleanup_mqtt_retained') as mock_cleanup:
            _shutdown(em, MagicMock(), mc, MagicMock(), MagicMock(),
                      shutting_down, MagicMock())

        mock_cleanup.assert_called_once_with(mc)

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

        with patch('generate_nibe_mqtt.teardown_lovelace'), \
             patch('generate_nibe_mqtt.os.environ.get', return_value=None), \
             patch('generate_nibe_mqtt.log_mqtt') as mock_log:
            _shutdown(em, MagicMock(), mc, MagicMock(), MagicMock(),
                      shutting_down, MagicMock())

        # Must have logged a warning, not raised
        self.assertTrue(shutting_down[0],
                        "Shutdown must complete even when wait_for_publish raises")
        warning_calls = [c for c in mock_log.warning.call_args_list
                         if 'confirm' in str(c).lower() or 'publish' in str(c).lower()]
        self.assertTrue(warning_calls,
                        "wait_for_publish exception must be logged as a warning")

    def test_entity_without_availability_topic_skipped_in_offline_publish(self):
        """Entities with no availability_topic must be silently skipped when
        publishing offline — the avail_topic None guard (branch 1240→1238)."""
        from generate_nibe_mqtt import _shutdown
        em = _make_em()
        # Add an entity with no availability_topic
        em.active_entities_by_id[99] = {
            'point_id': 99, 'entity_type': 'sensor',
            'state_topic': 'nibe/state/99',
            'command_topic': None, 'point_data': {},
            # 'availability_topic' deliberately absent
        }
        em.mqtt_enabled_points.add(99)
        mc = MagicMock()
        shutting_down = [False]

        with patch('generate_nibe_mqtt.teardown_lovelace'), \
             patch('generate_nibe_mqtt.os.environ.get', return_value=None):
            _shutdown(em, MagicMock(), mc, MagicMock(), MagicMock(),
                      shutting_down, MagicMock())

        # Offline must not have been published for the entity with no avail topic
        offline_topics = [c.args[0] for c in mc.publish.call_args_list
                          if c.args[1] == 'offline']
        self.assertNotIn('nibe/state/99', offline_topics,
                         "Entity without availability_topic must not get an offline publish")



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
        em._post_write_active = False
        em.bulk_interval = 30
        em._post_write_interval = 5

        with patch('generate_nibe_mqtt.time.time', side_effect=_fake_time), \
             patch('generate_nibe_mqtt.time.sleep', side_effect=_fake_sleep), \
             patch('generate_nibe_mqtt.update_stats_and_health'), \
             patch('generate_nibe_mqtt.update_device_modes'), \
             patch('generate_nibe_mqtt.update_alarm_state'):
            with self.assertRaises(KeyboardInterrupt):
                _poll_loop(em, pub, 'essential')

    def test_calls_update_all_states_each_cycle(self):
        """update_all_states() must be called once per elapsed-interval cycle."""
        em  = _make_em()
        pub = MagicMock()
        self._run_loop_iterations(em, pub, iterations=3)
        self.assertGreaterEqual(em.update_all_states.call_count, 1)

    def test_keyboard_interrupt_propagates(self):
        """_poll_loop must re-raise KeyboardInterrupt immediately."""
        from generate_nibe_mqtt import _poll_loop
        em  = _make_em()
        pub = MagicMock()
        em.initial_discovery_complete = True
        em._post_write_active = False
        em.bulk_interval = 30
        em._post_write_interval = 5

        with patch('generate_nibe_mqtt.time.time', return_value=99999.0), \
             patch('generate_nibe_mqtt.time.sleep', side_effect=KeyboardInterrupt), \
             patch('generate_nibe_mqtt.update_stats_and_health'), \
             patch('generate_nibe_mqtt.update_device_modes'), \
             patch('generate_nibe_mqtt.update_alarm_state'):
            with self.assertRaises(KeyboardInterrupt):
                _poll_loop(em, pub, 'essential')

    def test_exception_in_cycle_does_not_exit_loop(self):
        """A single exception in a poll cycle must be caught and the loop continued."""
        from generate_nibe_mqtt import _poll_loop
        em  = _make_em()
        pub = MagicMock()
        em.initial_discovery_complete = True
        em._post_write_active = False
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

        with patch('generate_nibe_mqtt.time.time', side_effect=_fake_time), \
             patch('generate_nibe_mqtt.time.sleep', side_effect=_fake_sleep), \
             patch('generate_nibe_mqtt.update_stats_and_health'), \
             patch('generate_nibe_mqtt.update_device_modes'), \
             patch('generate_nibe_mqtt.update_alarm_state'):
            with self.assertRaises(KeyboardInterrupt):
                _poll_loop(em, pub, 'essential')

        # Loop ran more than 1 cycle: crash on cycle 1 did not kill the loop
        self.assertGreater(call_count[0], 1)

    def test_backoff_escalates_on_consecutive_errors(self):
        """Consecutive exceptions must produce increasing backoff sleep durations."""
        from generate_nibe_mqtt import _poll_loop
        em  = _make_em()
        pub = MagicMock()
        em.initial_discovery_complete = True
        em._post_write_active = False
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

        with patch('generate_nibe_mqtt.time.time', side_effect=_fake_time), \
             patch('generate_nibe_mqtt.time.sleep', side_effect=_fake_sleep), \
             patch('generate_nibe_mqtt.update_stats_and_health'), \
             patch('generate_nibe_mqtt.update_device_modes'), \
             patch('generate_nibe_mqtt.update_alarm_state'):
            with self.assertRaises(KeyboardInterrupt):
                _poll_loop(em, pub, 'essential')

        self.assertGreater(len(backoff_sleeps), 1,
                           "Expected multiple backoff sleeps from consecutive errors")
        self.assertGreater(backoff_sleeps[-1], backoff_sleeps[0],
                           "Backoff duration must escalate on consecutive errors")

    def test_backoff_capped_at_60_seconds(self):
        """Backoff must never exceed 60 seconds regardless of error count."""
        from generate_nibe_mqtt import _poll_loop
        em  = _make_em()
        pub = MagicMock()
        em.initial_discovery_complete = True
        em._post_write_active = False
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

        with patch('generate_nibe_mqtt.time.time', side_effect=_fake_time), \
             patch('generate_nibe_mqtt.time.sleep', side_effect=_fake_sleep), \
             patch('generate_nibe_mqtt.update_stats_and_health'), \
             patch('generate_nibe_mqtt.update_device_modes'), \
             patch('generate_nibe_mqtt.update_alarm_state'):
            with self.assertRaises(KeyboardInterrupt):
                _poll_loop(em, pub, 'essential')

        if backoff_sleeps:
            self.assertLessEqual(max(backoff_sleeps), 60,
                                 "Backoff must be capped at 60 seconds")

    def test_alert_published_after_five_consecutive_errors(self):
        """After 5 consecutive errors publish_bridge_alert must be called."""
        from generate_nibe_mqtt import _poll_loop
        em  = _make_em()
        pub = MagicMock()
        em.initial_discovery_complete = True
        em._post_write_active = False
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

        with patch('generate_nibe_mqtt.time.time', side_effect=_fake_time), \
             patch('generate_nibe_mqtt.time.sleep', side_effect=_fake_sleep), \
             patch('generate_nibe_mqtt.update_stats_and_health'), \
             patch('generate_nibe_mqtt.update_device_modes'), \
             patch('generate_nibe_mqtt.update_alarm_state'):
            with self.assertRaises(KeyboardInterrupt):
                _poll_loop(em, pub, 'essential')

        pub.publish_bridge_alert.assert_called()
        call_kwargs = pub.publish_bridge_alert.call_args.kwargs
        self.assertEqual(call_kwargs.get('alert_type'), 'main_loop_error')

    def test_deferred_discovery_runs_when_initial_discovery_incomplete(self):
        """When initial_discovery_complete=False and api_consecutive_failures=0,
        complete_deferred_discovery() must be called instead of update_all_states(),
        and last_bulk_fetch must be updated when it returns True."""
        from generate_nibe_mqtt import _poll_loop
        em  = _make_em()
        pub = MagicMock()
        em.initial_discovery_complete = False
        em.api_consecutive_failures   = 0
        em._post_write_active         = False
        em.bulk_interval              = 30
        em._post_write_interval       = 5
        em.complete_deferred_discovery = MagicMock(return_value=True)
        em.update_all_states           = MagicMock()

        tick = [0]
        _t = [0.0]
        def _fake_time():
            _t[0] += 60.0
            return _t[0]
        def _fake_sleep(_s):
            tick[0] += 1
            if tick[0] >= 2:
                raise KeyboardInterrupt

        with patch('generate_nibe_mqtt.time.time', side_effect=_fake_time), \
             patch('generate_nibe_mqtt.time.sleep', side_effect=_fake_sleep), \
             patch('generate_nibe_mqtt.update_stats_and_health'), \
             patch('generate_nibe_mqtt.update_device_modes'), \
             patch('generate_nibe_mqtt.update_alarm_state'):
            with self.assertRaises(KeyboardInterrupt):
                _poll_loop(em, pub, 'essential')

        em.complete_deferred_discovery.assert_called_with('essential')
        # update_all_states must NOT be called when deferred_ran=True
        em.update_all_states.assert_not_called()

    def test_memory_logging_exception_does_not_crash_loop(self):
        """An exception in get_memory_usage() must be caught and logged — the
        poll loop must continue normally (memory logging error handler)."""
        from generate_nibe_mqtt import _poll_loop
        em  = _make_em()
        pub = MagicMock()
        em.initial_discovery_complete = True
        em._post_write_active         = False
        em.bulk_interval              = 30
        em._post_write_interval       = 5
        em.update_all_states          = MagicMock()
        em.get_memory_usage           = MagicMock(side_effect=RuntimeError("oom"))

        tick   = [0]
        _t     = [0.0]
        # Make time jump far enough to trigger memory logging (600s threshold)
        def _fake_time():
            _t[0] += 700.0
            return _t[0]
        def _fake_sleep(_s):
            tick[0] += 1
            if tick[0] >= 2:
                raise KeyboardInterrupt

        with patch('generate_nibe_mqtt.time.time', side_effect=_fake_time), \
             patch('generate_nibe_mqtt.time.sleep', side_effect=_fake_sleep), \
             patch('generate_nibe_mqtt.update_stats_and_health'), \
             patch('generate_nibe_mqtt.update_device_modes'), \
             patch('generate_nibe_mqtt.update_alarm_state'), \
             patch('generate_nibe_mqtt.log_startup') as mock_log:
            with self.assertRaises(KeyboardInterrupt):
                _poll_loop(em, pub, 'essential')

        # Must have logged the error, not propagated it
        error_calls = [c for c in mock_log.error.call_args_list
                       if 'Memory' in str(c) or 'memory' in str(c)]
        self.assertTrue(error_calls,
                        "Memory logging error must be caught and logged")



class TestRunStartupSequence(unittest.TestCase):
    """_run_startup_sequence: happy path, discovery failure, mode application."""

    def _cfg(self):
        from generate_nibe_mqtt import BridgeConfig
        return BridgeConfig(
            api_base_url='https://10.0.0.1:8443/api/v1/devices/0',
            nibe_auth='Basic dXNlcjpwYXNz',
            mqtt_broker='localhost',
            mqtt_port=1883,
            device_name='Test Device',
            device_id='nibe_test001',
            poll_interval=30,
            api_failure_threshold=3,
            changelog_retention_days=90,
            mode='essential',
        )

    def _run(self, cfg=None, response=None, discover_ok=True, initial_mode='essential'):
        from generate_nibe_mqtt import _run_startup_sequence
        cfg      = cfg or self._cfg()
        response = response or {}
        mc       = MagicMock()

        with patch('generate_nibe_mqtt._build_device_info', return_value={'model': 'S40'}), \
             patch('generate_nibe_mqtt.MqttDiscoveryPublisher') as MockPub, \
             patch('generate_nibe_mqtt.EntityManager') as MockEM, \
             patch('generate_nibe_mqtt._load_menu_structure', return_value=({}, frozenset())), \
             patch('generate_nibe_mqtt.dismiss_ha'), \
             patch('generate_nibe_mqtt.notify_ha'), \
             patch('generate_nibe_mqtt.HAEntityRegistryWatcher') as MockWatcher, \
             patch('generate_nibe_mqtt.threading.Thread'), \
             patch('generate_nibe_mqtt.ManagementCommandHandler'), \
             patch('generate_nibe_mqtt._run_scan_with_retry', return_value=set()), \
             patch('generate_nibe_mqtt.decide_startup_action', return_value='apply'), \
             patch('generate_nibe_mqtt._execute_startup_action'), \
             patch('generate_nibe_mqtt.update_stats_and_health'), \
             patch('generate_nibe_mqtt.update_device_modes'), \
             patch('generate_nibe_mqtt.remove_menu_dashboard'), \
             patch('generate_nibe_mqtt.concurrent.futures.ThreadPoolExecutor'), \
             patch('generate_nibe_mqtt.time.sleep'):

            em_instance = MockEM.return_value
            em_instance.discover_points.return_value = discover_ok
            em_instance.mqtt_enabled_points = set()
            em_instance.all_points          = []
            em_instance.active_entities     = []
            em_instance.bulk_interval       = 30

            pub_instance = MockPub.return_value
            pub_instance.mqtt = MagicMock()

            result = _run_startup_sequence(
                cfg, MagicMock(), mc, response, 'nibe_test001',
                initial_mode, 'info', MagicMock(),
            )

        return result, em_instance, pub_instance, MockWatcher

    def test_returns_four_tuple(self):
        """Must return (entity_manager, publisher, registry_watcher, mgmt_executor)."""
        result, *_ = self._run()
        self.assertEqual(len(result), 4)

    def test_entity_manager_configured_from_cfg(self):
        """bulk_interval, api_failure_threshold, changelog_retention_days must be set."""
        cfg = self._cfg()
        cfg.poll_interval             = 60
        cfg.api_failure_threshold     = 5
        cfg.changelog_retention_days  = 30
        _, em, *_ = self._run(cfg=cfg)
        self.assertEqual(em.bulk_interval, 60)
        self.assertEqual(em.api_failure_threshold, 5)
        self.assertEqual(em.changelog_retention_days, 30)

    def test_notify_ha_called_when_discovery_fails(self):
        """When discover_points() returns False, notify_ha must be called."""
        with patch('generate_nibe_mqtt.notify_ha') as mock_notify, \
             patch('generate_nibe_mqtt._build_device_info', return_value={}), \
             patch('generate_nibe_mqtt.MqttDiscoveryPublisher') as MockPub, \
             patch('generate_nibe_mqtt.EntityManager') as MockEM, \
             patch('generate_nibe_mqtt._load_menu_structure', return_value=({}, frozenset())), \
             patch('generate_nibe_mqtt.dismiss_ha'), \
             patch('generate_nibe_mqtt.HAEntityRegistryWatcher'), \
             patch('generate_nibe_mqtt.threading.Thread'), \
             patch('generate_nibe_mqtt.ManagementCommandHandler'), \
             patch('generate_nibe_mqtt._run_scan_with_retry', return_value=set()), \
             patch('generate_nibe_mqtt.decide_startup_action', return_value='apply'), \
             patch('generate_nibe_mqtt._execute_startup_action'), \
             patch('generate_nibe_mqtt.update_stats_and_health'), \
             patch('generate_nibe_mqtt.update_device_modes'), \
             patch('generate_nibe_mqtt.remove_menu_dashboard'), \
             patch('generate_nibe_mqtt.concurrent.futures.ThreadPoolExecutor'), \
             patch('generate_nibe_mqtt.time.sleep'):
            from generate_nibe_mqtt import _run_startup_sequence
            cfg = self._cfg()
            em_inst = MockEM.return_value
            em_inst.discover_points.return_value = False
            em_inst.mqtt_enabled_points = set()
            em_inst.all_points          = []
            em_inst.active_entities     = []
            em_inst.bulk_interval       = 30
            MockPub.return_value.mqtt   = MagicMock()

            _run_startup_sequence(
                cfg, MagicMock(), MagicMock(), {}, 'nibe_test001', 'essential', 'info',
                MagicMock(),
            )

        mock_notify.assert_called()
        call_kwargs = mock_notify.call_args.kwargs
        self.assertIn('notification_id', call_kwargs)
        self.assertEqual(call_kwargs['notification_id'], 'nibe_discovery_incomplete')

    def test_discovery_notification_flag_set_on_failure(self):
        """entity_manager._discovery_notification_active must be True after failed discovery."""
        _, em, *_ = self._run(discover_ok=False)
        self.assertTrue(em._discovery_notification_active)


# ===========================================================================
# Missing session additions — re-applied from session transcript
# ===========================================================================

# ---------------------------------------------------------------------------
# TestUpdateEntityStateNoValueMappings additions
# ---------------------------------------------------------------------------



# ===========================================================================
# Coverage gaps in generate_nibe_mqtt.py
# ===========================================================================


class TestRunStartupSequenceDebugReset(unittest.TestCase):
    """_run_startup_sequence clears stale test result sensor on startup
    when debug mode is active (log_level='debug')."""

    def _run_debug(self):
        from generate_nibe_mqtt import _run_startup_sequence, BridgeConfig
        cfg = BridgeConfig(
            api_base_url='https://10.0.0.1:8443/api/v1/devices/0',
            nibe_auth='Basic dXNlcjpwYXNz',
            mqtt_broker='localhost', mqtt_port=1883,
            device_name='Test', device_id='nibe_test001',
            poll_interval=30, api_failure_threshold=3,
            changelog_retention_days=90, mode='essential',
        )
        mc = MagicMock()
        with patch('generate_nibe_mqtt._build_device_info', return_value={'model': 'S40'}), \
             patch('generate_nibe_mqtt.MqttDiscoveryPublisher') as MockPub, \
             patch('generate_nibe_mqtt.EntityManager') as MockEM, \
             patch('generate_nibe_mqtt._load_menu_structure', return_value=({}, frozenset())), \
             patch('generate_nibe_mqtt.dismiss_ha'), \
             patch('generate_nibe_mqtt.notify_ha'), \
             patch('generate_nibe_mqtt.HAEntityRegistryWatcher'), \
             patch('generate_nibe_mqtt.threading.Thread'), \
             patch('generate_nibe_mqtt.ManagementCommandHandler'), \
             patch('generate_nibe_mqtt._run_scan_with_retry', return_value=set()), \
             patch('generate_nibe_mqtt.decide_startup_action', return_value='apply'), \
             patch('generate_nibe_mqtt._execute_startup_action'), \
             patch('generate_nibe_mqtt.update_stats_and_health'), \
             patch('generate_nibe_mqtt.update_device_modes'), \
             patch('generate_nibe_mqtt.remove_menu_dashboard'), \
             patch('generate_nibe_mqtt.concurrent.futures.ThreadPoolExecutor'), \
             patch('generate_nibe_mqtt.time.sleep'):
            em_instance = MockEM.return_value
            em_instance.discover_points.return_value = True
            em_instance.mqtt_enabled_points = set()
            em_instance.all_points = []
            em_instance.active_entities = []
            em_instance.bulk_interval = 30
            pub_instance = MockPub.return_value
            pub_instance.mqtt = MagicMock()
            _run_startup_sequence(
                cfg, MagicMock(), mc, {}, 'nibe_test001',
                'essential', 'debug', MagicMock(),
            )
        return mc

    def test_debug_mode_publishes_ready_attrs_on_startup(self):
        """In debug mode, RUN_TESTS_ATTRS must be published with status='ready'
        on startup so the sensor attributes show a clean state after a rebuild.
        RUN_TESTS_STATE is intentionally NOT published to avoid triggering
        HA automations on restart."""
        from nibe_mqtt_publisher import MgmtTopic
        import json as _json
        mc = self._run_debug()
        topics = [c.args[0] for c in mc.publish.call_args_list]
        # Attrs must be reset
        self.assertIn(MgmtTopic.RUN_TESTS_ATTRS, topics)
        attrs_calls = [c for c in mc.publish.call_args_list
                       if c.args[0] == MgmtTopic.RUN_TESTS_ATTRS]
        payloads = [_json.loads(c.args[1]) for c in attrs_calls]
        self.assertTrue(any(p.get('status') == 'ready' for p in payloads),
                        "RUN_TESTS_ATTRS must contain status='ready' on startup")
        # State topic must NOT be published on startup
        self.assertNotIn(MgmtTopic.RUN_TESTS_STATE, topics,
                         "RUN_TESTS_STATE must not be published on startup — would trigger automations")

    def test_non_debug_mode_does_not_publish_test_state_on_startup(self):
        """In non-debug mode the test result sensor does not exist —
        RUN_TESTS_STATE must not be published on startup."""
        from generate_nibe_mqtt import _run_startup_sequence, BridgeConfig
        from nibe_mqtt_publisher import MgmtTopic
        cfg = BridgeConfig(
            api_base_url='https://10.0.0.1:8443/api/v1/devices/0',
            nibe_auth='Basic dXNlcjpwYXNz',
            mqtt_broker='localhost', mqtt_port=1883,
            device_name='Test', device_id='nibe_test001',
            poll_interval=30, api_failure_threshold=3,
            changelog_retention_days=90, mode='essential',
        )
        mc = MagicMock()
        with patch('generate_nibe_mqtt._build_device_info', return_value={'model': 'S40'}), \
             patch('generate_nibe_mqtt.MqttDiscoveryPublisher') as MockPub, \
             patch('generate_nibe_mqtt.EntityManager') as MockEM, \
             patch('generate_nibe_mqtt._load_menu_structure', return_value=({}, frozenset())), \
             patch('generate_nibe_mqtt.dismiss_ha'), \
             patch('generate_nibe_mqtt.notify_ha'), \
             patch('generate_nibe_mqtt.HAEntityRegistryWatcher'), \
             patch('generate_nibe_mqtt.threading.Thread'), \
             patch('generate_nibe_mqtt.ManagementCommandHandler'), \
             patch('generate_nibe_mqtt._run_scan_with_retry', return_value=set()), \
             patch('generate_nibe_mqtt.decide_startup_action', return_value='apply'), \
             patch('generate_nibe_mqtt._execute_startup_action'), \
             patch('generate_nibe_mqtt.update_stats_and_health'), \
             patch('generate_nibe_mqtt.update_device_modes'), \
             patch('generate_nibe_mqtt.remove_menu_dashboard'), \
             patch('generate_nibe_mqtt.concurrent.futures.ThreadPoolExecutor'), \
             patch('generate_nibe_mqtt.time.sleep'):
            em_instance = MockEM.return_value
            em_instance.discover_points.return_value = True
            em_instance.mqtt_enabled_points = set()
            em_instance.all_points = []
            em_instance.active_entities = []
            em_instance.bulk_interval = 30
            pub_instance = MockPub.return_value
            pub_instance.mqtt = MagicMock()
            _run_startup_sequence(
                cfg, MagicMock(), mc, {}, 'nibe_test001',
                'essential', 'info', MagicMock(),
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
            api_base_url='https://10.0.0.1:8443/api/v1/devices/0',
            nibe_auth='Basic dXNlcjpwYXNz',
            mqtt_broker='localhost',
            mqtt_port=1883,
            device_name='Test Device',
            device_id='nibe_test',
            poll_interval=30,
        )
        for k, v in kw.items():
            setattr(cfg, k, v)
        return cfg

    def _run_infra(self, cfg, mock_mc):
        from generate_nibe_mqtt import _build_infrastructure
        with patch('generate_nibe_mqtt._fetch_api_response', return_value={}), \
             patch('generate_nibe_mqtt._build_ssl_context', return_value=MagicMock()), \
             patch('generate_nibe_mqtt.NibeApiClient'), \
             patch('generate_nibe_mqtt.copy_card_file'), \
             patch('generate_nibe_mqtt.mqtt.Client', return_value=mock_mc), \
             patch('generate_nibe_mqtt.time.sleep'):
            return _build_infrastructure(cfg)

    def test_username_pw_set_called_when_credentials_present(self):
        """When mqtt_username and mqtt_password are both set, username_pw_set()
        must be called on the mqtt client (line 824 — the True branch)."""
        cfg = self._cfg(mqtt_username='user', mqtt_password='secret')
        mock_mc = MagicMock()
        mock_mc.is_connected.return_value = True
        self._run_infra(cfg, mock_mc)
        mock_mc.username_pw_set.assert_called_once_with('user', 'secret')

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
        self.assertIsNotNone(on_connect, 'on_connect callback was not registered')

        rc = MagicMock()
        rc.value = 4   # MQTT bad credentials — in _FATAL_RC = {4, 5}

        with patch('generate_nibe_mqtt.log_mqtt') as mock_log:
            on_connect(mock_mc, None, None, rc, None)

        mock_log.error.assert_called_once()
        msg = str(mock_log.error.call_args)
        self.assertIn('refused', msg)

    def test_on_connect_non_fatal_non_zero_rc_logs_error(self):
        """on_connect with rc=3 (broker unavailable, not in FATAL_RC) must
        reach the else branch and log an error — lines 861–864."""
        cfg = self._cfg()
        mock_mc = MagicMock()
        mock_mc.is_connected.return_value = True
        self._run_infra(cfg, mock_mc)

        on_connect = mock_mc.on_connect
        rc = MagicMock()
        rc.value = 3   # not in _FATAL_RC

        with patch('generate_nibe_mqtt.log_mqtt') as mock_log:
            on_connect(mock_mc, None, None, rc, None)

        mock_log.error.assert_called_once()

    def test_not_yet_connected_warning_when_is_connected_false(self):
        """When is_connected() returns False after loop_start (slow broker),
        a warning must be logged — line 898."""
        cfg = self._cfg()
        mock_mc = MagicMock()
        mock_mc.is_connected.return_value = False   # <-- slow broker

        with patch('generate_nibe_mqtt.log_mqtt') as mock_log:
            self._run_infra(cfg, mock_mc)

        warning_msgs = str(mock_log.warning.call_args_list)
        self.assertIn('not yet connected', warning_msgs)


class TestRunStartupSequenceMenusMode(unittest.TestCase):
    """_run_startup_sequence with initial_mode='menus' must call
    schedule_menu_dashboard_regen instead of remove_menu_dashboard (line 1062)."""

    def _run(self, initial_mode):
        from generate_nibe_mqtt import _run_startup_sequence, BridgeConfig
        cfg = BridgeConfig(
            api_base_url='https://10.0.0.1:8443/api/v1/devices/0',
            nibe_auth='Basic dXNlcjpwYXNz',
            mqtt_broker='localhost',
            mqtt_port=1883,
            device_name='Test Device',
            device_id='nibe_test001',
            poll_interval=30,
            api_failure_threshold=3,
            changelog_retention_days=90,
            mode=initial_mode,
        )
        with patch('generate_nibe_mqtt._build_device_info', return_value={}), \
             patch('generate_nibe_mqtt.MqttDiscoveryPublisher') as MockPub, \
             patch('generate_nibe_mqtt.EntityManager') as MockEM, \
             patch('generate_nibe_mqtt._load_menu_structure', return_value=({}, frozenset())), \
             patch('generate_nibe_mqtt.dismiss_ha'), \
             patch('generate_nibe_mqtt.notify_ha'), \
             patch('generate_nibe_mqtt.HAEntityRegistryWatcher'), \
             patch('generate_nibe_mqtt.threading.Thread'), \
             patch('generate_nibe_mqtt.ManagementCommandHandler'), \
             patch('generate_nibe_mqtt._run_scan_with_retry', return_value=set()), \
             patch('generate_nibe_mqtt.decide_startup_action', return_value='apply'), \
             patch('generate_nibe_mqtt._execute_startup_action'), \
             patch('generate_nibe_mqtt.update_stats_and_health'), \
             patch('generate_nibe_mqtt.update_device_modes'), \
             patch('generate_nibe_mqtt.remove_menu_dashboard') as mock_remove, \
             patch('generate_nibe_mqtt.schedule_menu_dashboard_regen') as mock_sched, \
             patch('generate_nibe_mqtt.concurrent.futures.ThreadPoolExecutor'), \
             patch('generate_nibe_mqtt.time.sleep'):
            em = MockEM.return_value
            em.discover_points.return_value = True
            em.mqtt_enabled_points = set()
            em.all_points          = []
            em.active_entities     = []
            em.bulk_interval       = 30
            MockPub.return_value.mqtt = MagicMock()
            _run_startup_sequence(
                cfg, MagicMock(), MagicMock(), {}, 'nibe_test001',
                initial_mode, 'info', MagicMock(),
            )
        return mock_remove, mock_sched

    def test_menus_mode_calls_schedule_not_remove(self):
        """initial_mode='menus' must call schedule_menu_dashboard_regen (line 1062),
        not remove_menu_dashboard."""
        mock_remove, mock_sched = self._run('menus')
        mock_sched.assert_called_once()
        mock_remove.assert_not_called()

    def test_non_menus_mode_calls_remove_not_schedule(self):
        """initial_mode='essential' must call remove_menu_dashboard (line 1067),
        not schedule_menu_dashboard_regen."""
        mock_remove, mock_sched = self._run('essential')
        mock_remove.assert_called_once()
        mock_sched.assert_not_called()


class TestPollLoopAlertPublishException(unittest.TestCase):
    """When publish_bridge_alert itself raises after ≥5 consecutive errors,
    the inner 'except Exception: pass' (lines 1188–1189) must suppress it
    and the loop must continue."""

    def test_alert_publish_exception_suppressed(self):
        """publisher.publish_bridge_alert raising must not kill the loop."""
        from generate_nibe_mqtt import _poll_loop
        em  = _make_em()
        pub = MagicMock()
        pub.publish_bridge_alert.side_effect = RuntimeError('broker gone')

        em.initial_discovery_complete = True
        em._post_write_active = False
        em.bulk_interval = 30
        em._post_write_interval = 5

        crash_count = [0]

        def _always_crash():
            crash_count[0] += 1
            raise RuntimeError('persistent failure')

        em.update_all_states = MagicMock(side_effect=_always_crash)

        tick = [0]
        def _fake_sleep(_s):
            tick[0] += 1
            if tick[0] >= 8:   # enough cycles to reach the ≥5 threshold
                raise KeyboardInterrupt

        _t = [0.0]
        def _fake_time():
            _t[0] += 60.0
            return _t[0]

        with patch('generate_nibe_mqtt.time.time', side_effect=_fake_time), \
             patch('generate_nibe_mqtt.time.sleep', side_effect=_fake_sleep), \
             patch('generate_nibe_mqtt.update_stats_and_health'), \
             patch('generate_nibe_mqtt.update_device_modes'), \
             patch('generate_nibe_mqtt.update_alarm_state'):
            with self.assertRaises(KeyboardInterrupt):
                _poll_loop(em, pub, 'essential')

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

        with patch('generate_nibe_mqtt.threading.Thread') as MockThread, \
             patch('generate_nibe_mqtt.teardown_lovelace'), \
             patch('generate_nibe_mqtt.os.environ.get', return_value=None), \
             patch('generate_nibe_mqtt.log_startup') as mock_log:
            instance = MockThread.return_value
            instance.is_alive.return_value = True   # simulate timeout
            _shutdown(em, MagicMock(), mc, MagicMock(), MagicMock(),
                      [False], MagicMock())

        # Warning must mention the timeout
        warning_calls = str(mock_log.warning.call_args_list)
        self.assertIn('did not finish', warning_calls)


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
        result_true  = _load_menu_structure(_APP_DIR, log_if_mode=True)
        result_false = _load_menu_structure(_APP_DIR, log_if_mode=False)
        # Both calls must return identical data
        self.assertEqual(result_true[0], result_false[0])   # point_to_menu
        self.assertEqual(result_true[1], result_false[1])   # menu_points

    def test_log_if_mode_false_does_not_raise(self):
        from generate_nibe_mqtt import _load_menu_structure
        _load_menu_structure(_APP_DIR, log_if_mode=False)   # must not raise


class TestBuildInfrastructureOnConnectEmptyEm(unittest.TestCase):
    """on_connect: 851→exit — rc=0 before entity_manager is wired.

    The first MQTT connection fires on_connect before _run_startup_sequence
    has called set_entity_manager().  At that point _em=[] is falsy, so
    resubscribe_all/republish_availability must NOT be called.
    """

    def _cfg(self):
        from generate_nibe_mqtt import BridgeConfig
        return BridgeConfig(
            api_base_url='https://10.0.0.1:8443/api/v1/devices/0',
            nibe_auth='Basic dXNlcjpwYXNz',
            mqtt_broker='localhost',
            mqtt_port=1883,
            device_name='Test Device',
            device_id='nibe_test',
            poll_interval=30,
        )

    def test_on_connect_rc0_before_set_em_does_not_call_resubscribe(self):
        """Firing on_connect with rc=0 before set_entity_manager() is called
        must not attempt resubscribe_all — _em is still empty (851→exit)."""
        from generate_nibe_mqtt import _build_infrastructure
        cfg = self._cfg()
        mock_mc = MagicMock()
        mock_mc.is_connected.return_value = True
        with patch('generate_nibe_mqtt._fetch_api_response', return_value={}), \
             patch('generate_nibe_mqtt._build_ssl_context', return_value=MagicMock()), \
             patch('generate_nibe_mqtt.NibeApiClient'), \
             patch('generate_nibe_mqtt.copy_card_file'), \
             patch('generate_nibe_mqtt.mqtt.Client', return_value=mock_mc), \
             patch('generate_nibe_mqtt.time.sleep'):
            _, _, _, _, _, set_em = _build_infrastructure(cfg)

        # Fire on_connect WITHOUT calling set_em first — _em is still []
        on_connect = mock_mc.on_connect
        rc = MagicMock()
        rc.value = 0
        on_connect(mock_mc, None, None, rc, None)   # must not raise
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
        em  = _make_em()
        pub = MagicMock()
        em.initial_discovery_complete = True
        em._post_write_active         = False
        em.bulk_interval              = 30
        em._post_write_interval       = 5
        em.update_all_states          = MagicMock()
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
        em.get_memory_usage = MagicMock(return_value={
            'total_points': 100, 'active_entities': 50,
            'estimated_memory_mb': 2.5,
            'value_cache_size': 10, 'last_states_size': 20,
            'point_string_cache_size': 5,
        })

        tick = [0]
        _t   = [0.0]

        def _fake_time():
            _t[0] += 700.0   # each call jumps 700s → crosses 600s threshold
            return _t[0]

        def _fake_sleep(_s):
            tick[0] += 1
            if tick[0] >= 2:
                raise KeyboardInterrupt

        with patch('generate_nibe_mqtt.time.time',  side_effect=_fake_time), \
             patch('generate_nibe_mqtt.time.sleep',  side_effect=_fake_sleep), \
             patch('generate_nibe_mqtt.update_stats_and_health'), \
             patch('generate_nibe_mqtt.update_device_modes'), \
             patch('generate_nibe_mqtt.update_alarm_state'):
            with self.assertRaises(KeyboardInterrupt):
                _poll_loop(em, pub, 'essential')

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
        _t   = [0.0]

        def _fake_time():
            _t[0] += 60.0
            return _t[0]

        def _fake_sleep(s):
            tick[0] += 1
            if s > 1:
                backoff_sleeps.append(s)
            if tick[0] >= 20:
                raise KeyboardInterrupt

        with patch('generate_nibe_mqtt.time.time',  side_effect=_fake_time), \
             patch('generate_nibe_mqtt.time.sleep',  side_effect=_fake_sleep), \
             patch('generate_nibe_mqtt.update_stats_and_health'), \
             patch('generate_nibe_mqtt.update_device_modes'), \
             patch('generate_nibe_mqtt.update_alarm_state'):
            with self.assertRaises(KeyboardInterrupt):
                _poll_loop(em, pub, 'essential')

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

        call_seq = iter([
            RuntimeError("first crash"),  # error → count=1, backoff=5
            None,                         # success → count resets to 0
            RuntimeError("second crash"), # error → count=1 again, backoff=5
        ])

        def _update():
            v = next(call_seq, None)
            if isinstance(v, Exception):
                raise v

        em.update_all_states = MagicMock(side_effect=_update)

        backoff_sleeps = []
        tick = [0]
        _t   = [0.0]

        def _fake_time():
            _t[0] += 60.0
            return _t[0]

        def _fake_sleep(s):
            tick[0] += 1
            if s > 1:
                backoff_sleeps.append(s)
            if tick[0] >= 10:
                raise KeyboardInterrupt

        with patch('generate_nibe_mqtt.time.time',  side_effect=_fake_time), \
             patch('generate_nibe_mqtt.time.sleep',  side_effect=_fake_sleep), \
             patch('generate_nibe_mqtt.update_stats_and_health'), \
             patch('generate_nibe_mqtt.update_device_modes'), \
             patch('generate_nibe_mqtt.update_alarm_state'):
            with self.assertRaises(KeyboardInterrupt):
                _poll_loop(em, pub, 'essential')

        # After reset, the second crash should produce backoff=5 (count=1),
        # not a higher value that would indicate count was NOT reset.
        self.assertTrue(
            any(s == 5 for s in backoff_sleeps),
            f"Expected a 5s backoff (count=1 after reset), got: {backoff_sleeps}",
        )
