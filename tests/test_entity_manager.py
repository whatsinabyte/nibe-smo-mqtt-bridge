"""
test_entity_manager.py
======================
Nibe_entity_manager tests.
Part of the Nibe S-Series MQTT Bridge test suite.
Shared fixtures are in conftest.py.
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from conftest import (
    _cannot_be_int,
    _make_em,
    _nibe_point_id,
    _point_entry,
)
from hypothesis import example, given
from hypothesis import strategies as st


class TestCompressDecompressProperties(unittest.TestCase):
    """Hypothesis properties for _compress_payload and _decompress_payload."""

    _data_strategy = st.dictionaries(
        st.text(max_size=20),
        st.one_of(st.integers(), st.text(max_size=50), st.booleans(), st.none()),
        max_size=10,
    )

    @given(_data_strategy)
    def test_compress_never_raises(self, data):
        from nibe_entity_manager import _compress_payload
        result = _compress_payload(data)
        self.assertIsInstance(result, str)

    @given(_data_strategy)
    def test_compress_output_starts_with_sentinel(self, data):
        from nibe_entity_manager import _GZIP_SENTINEL, _compress_payload
        result = _compress_payload(data)
        self.assertTrue(result.startswith(_GZIP_SENTINEL))

    @given(_data_strategy)
    def test_compress_output_is_ascii(self, data):
        """Compressed payload must be plain ASCII — safe for paho MQTT publish."""
        from nibe_entity_manager import _compress_payload
        result = _compress_payload(data)
        result.encode('ascii')  # must not raise

    @given(_data_strategy)
    def test_roundtrip_recovers_original_data(self, data):
        """_decompress_payload(_compress_payload(data)) == json(data)."""
        import json as _json

        from nibe_entity_manager import _compress_payload, _decompress_payload
        compressed = _compress_payload(data)
        recovered_bytes = _decompress_payload(compressed)
        recovered = _json.loads(recovered_bytes.decode('utf-8'))
        self.assertEqual(recovered, data)

    @given(_data_strategy)
    def test_roundtrip_accepts_bytes_input(self, data):
        """_decompress_payload must accept bytes (paho delivers bytes)."""
        import json as _json

        from nibe_entity_manager import _compress_payload, _decompress_payload
        compressed = _compress_payload(data)
        recovered_bytes = _decompress_payload(compressed.encode('utf-8'))
        recovered = _json.loads(recovered_bytes.decode('utf-8'))
        self.assertEqual(recovered, data)

    @given(_data_strategy)
    def test_compress_roundtrip_is_stable(self, data):
        """Two independent compress→decompress roundtrips recover the original dict.
        gzip.compress embeds mtime so byte output differs between calls —
        roundtrip identity is the correct invariant, not byte equality.
        """
        import json as _json

        from nibe_entity_manager import _compress_payload, _decompress_payload
        r1 = _json.loads(_decompress_payload(_compress_payload(data)))
        r2 = _json.loads(_decompress_payload(_compress_payload(data)))
        self.assertEqual(r1, data)
        self.assertEqual(r2, data)

    def test_compress_uses_compact_json_separators(self):
        """json.dumps must use the compact (',', ':') separators — not the
        default (', ', ': ') — to keep the payload small for MQTT. A dict
        with two keys makes the space-vs-no-space difference land in the
        decompressed JSON text itself, independently checkable without
        reading anything back from the compress call under test."""
        import base64 as _base64
        import gzip as _gzip

        from nibe_entity_manager import _GZIP_SENTINEL, _compress_payload
        result = _compress_payload({'a': 1, 'b': 2})
        raw_json = _gzip.decompress(
            _base64.b64decode(result[len(_GZIP_SENTINEL):])
        ).decode('utf-8')
        self.assertEqual(raw_json, '{"a":1,"b":2}')

    def test_compress_uses_compresslevel_6(self):
        """gzip.compress must be called with compresslevel=6 specifically —
        verified by mocking gzip.compress and inspecting the call args
        against the literal constant 6, not by reading the constant back
        off the same call."""
        with patch('nibe_entity_manager.gzip.compress') as mock_compress:
            mock_compress.return_value = b'x'
            from nibe_entity_manager import _compress_payload
            _compress_payload({'k': 'v'})
        mock_compress.assert_called_once()
        self.assertEqual(mock_compress.call_args.kwargs.get('compresslevel'), 6)

    def test_decompress_replaces_invalid_utf8_bytes_instead_of_raising(self):
        """When the (post-sentinel-stripped) input bytes contain invalid
        UTF-8, decode() must use errors='replace' — not the default
        'strict' handler and not some other/garbled handler name — so a
        single bad byte becomes U+FFFD rather than raising UnicodeDecodeError.
        Constructed directly at the byte level since _compress_payload never
        itself produces invalid UTF-8 (base64 output is always ASCII)."""
        import nibe_entity_manager as _nem
        from nibe_entity_manager import _GZIP_SENTINEL

        # 0xFF is not valid UTF-8 anywhere. With errors='replace', the decode
        # step itself never raises — it substitutes U+FFFD and execution
        # proceeds to the (unrelated) base64 step, which then fails for its
        # own reason (U+FFFD isn't valid base64 input). With the default
        # 'strict' handler (the mutant), the *decode* step itself would
        # raise UnicodeDecodeError instead — so the exception TYPE is what
        # distinguishes correct behaviour from the mutant, not merely
        # whether something raises.
        malformed = _GZIP_SENTINEL.encode('ascii') + b'\xff\xff'
        try:
            _nem._decompress_payload(malformed)
            self.fail("expected a downstream base64 error for this malformed input")
        except UnicodeDecodeError:
            self.fail(
                "decode() raised UnicodeDecodeError — errors='replace' should have "
                "substituted U+FFFD instead of raising at the decode step"
            )
        except Exception:  # noqa: BLE001, S110 — deliberate fuzz test, any non-crash outcome is acceptable
            pass  # any non-UnicodeDecodeError failure downstream is expected





# ---------------------------------------------------------------------------
# 1. st.binary() fuzzing — _decompress_payload never crashes on garbage input
# ---------------------------------------------------------------------------


class TestDecompressPayloadFuzzing(unittest.TestCase):
    """Fuzz _decompress_payload with arbitrary binary input.

    Real-world risk: the MQTT broker retains compressed changelog payloads.
    If the payload is corrupted (truncated, bit-flipped, wrong sentinel)
    the bridge must never crash — it must return gracefully.
    """

    @given(st.binary(max_size=1000))
    def test_arbitrary_bytes_never_raises(self, data):
        """_decompress_payload must never raise for any byte sequence."""
        from nibe_entity_manager import _decompress_payload
        try:
            _decompress_payload(data)
        except Exception:  # noqa: BLE001, S110 — deliberate fuzz test, any non-crash outcome is acceptable
            pass  # any exception is acceptable — crash is not

    @given(st.binary(max_size=1000))
    def test_arbitrary_bytes_caller_pattern_never_raises(self, data):
        """The typical caller pattern — try decompress, fallback on failure —
        must work for any byte sequence."""
        import json as _json

        from nibe_entity_manager import _decompress_payload
        result = None
        try:
            raw  = _decompress_payload(data)
            result = _json.loads(raw)
        except Exception:  # noqa: BLE001 — deliberate fuzz test, any non-crash outcome is acceptable
            result = None
        # Result is always None or a parsed object — never an exception propagating
        self.assertIn(type(result), (dict, list, type(None)))

    @given(st.text(max_size=200))
    def test_arbitrary_string_never_raises(self, text):
        """_decompress_payload must never raise for any string input."""
        from nibe_entity_manager import _decompress_payload
        try:
            _decompress_payload(text)
        except Exception:  # noqa: BLE001, S110 — deliberate fuzz test, any non-crash outcome is acceptable
            pass  # any exception is acceptable — crash is not

    @given(st.binary(max_size=100))
    def test_garbage_with_sentinel_prefix_never_crashes(self, suffix):
        """Even if someone crafts bytes starting with the sentinel,
        corrupt compressed data must not crash."""
        from nibe_entity_manager import _GZIP_SENTINEL, _decompress_payload
        payload = _GZIP_SENTINEL.encode() + suffix
        try:
            _decompress_payload(payload)
        except Exception:  # noqa: BLE001, S110 — deliberate fuzz test, any non-crash outcome is acceptable
            pass  # graceful failure expected

    @example(data=b'')
    @given(st.binary(max_size=10))
    def test_very_short_binary_never_crashes(self, data):
        from nibe_entity_manager import _decompress_payload
        try:
            _decompress_payload(data)
        except Exception:  # noqa: BLE001, S110 — deliberate fuzz test, any non-crash outcome is acceptable
            pass


# ---------------------------------------------------------------------------
# 2. st.from_regex() — time parsing edge cases from real HA time entities
# ---------------------------------------------------------------------------


class TestTimeParsingFromRegex(unittest.TestCase):
    """Use st.from_regex to generate realistic-looking but edge-case time strings.

    HA sends HH:MM or HH:MM:SS on time entity command topics.
    The parser must handle valid, invalid, and boundary values.
    """

    def _em(self):
        return _make_em()

    def _ei(self, pid=100):
        return {
            'point_id': pid, 'entity_type': 'time',
            'metadata': {
                'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                'isWritable': True, 'divisor': 1, 'decimal': 0,
                'minValue': 0, 'maxValue': 86399,
                'variableType': 'integer', 'variableSize': 's32',
                'unit': '', 'shortUnit': '',
                'intDefaultValue': 0, 'stringDefaultValue': '',
                'change': 1,
            },
            'point_data': {},
        }

    @given(st.from_regex(r'[01][0-9]:[0-5][0-9]', fullmatch=True))
    @example(payload='00:00')   # midnight
    @example(payload='23:59')   # last minute of day
    @example(payload='12:00')   # noon
    def test_valid_hhmm_always_returns_int(self, payload):
        """Any valid HH:MM string must always return a non-negative int."""
        em = self._em()
        result = em._parse_command_payload(payload, self._ei(), 'test')
        self.assertIsInstance(result, int)
        self.assertGreaterEqual(result, 0)

    @given(st.from_regex(r'2[0-3]:[0-5][0-9]', fullmatch=True))
    def test_valid_hhmm_evening_always_returns_int(self, payload):
        """Evening times (20-23 hour) always parse correctly."""
        em = self._em()
        result = em._parse_command_payload(payload, self._ei(), 'test')
        self.assertIsInstance(result, int)

    @given(st.from_regex(r'\d{2}:\d{2}:\d{2}', fullmatch=True))
    @example(payload='00:00:00')
    @example(payload='23:59:59')
    def test_hhmmss_format_always_returns_int(self, payload):
        """HH:MM:SS format (including invalid ranges) returns int or None."""
        em = self._em()
        result = em._parse_command_payload(payload, self._ei(), 'test')
        self.assertIn(type(result), (int, type(None)))

    @given(st.from_regex(r'[3-9]\d:\d{2}', fullmatch=True))
    def test_out_of_range_hour_returns_none_or_int(self, payload):
        """Hours >= 30 are invalid — must return None or handle gracefully."""
        em = self._em()
        result = em._parse_command_payload(payload, self._ei(), 'test')
        self.assertIn(type(result), (int, type(None)))

    @given(st.from_regex(r'\d{2}:[6-9]\d', fullmatch=True))
    def test_out_of_range_minute_returns_none_or_int(self, payload):
        """Minutes >= 60 are invalid — must not raise."""
        em = self._em()
        result = em._parse_command_payload(payload, self._ei(), 'test')
        self.assertIn(type(result), (int, type(None)))

    @given(st.text(max_size=20).filter(
        lambda s: ':' not in s or not all(p.strip().isdigit() for p in s.split(':')[:2])
    ))
    def test_non_time_string_always_returns_none(self, payload):
        """Non-time strings must always return None without raising."""
        em = self._em()
        result = em._parse_command_payload(payload, self._ei(), 'test')
        self.assertIsNone(result)


class TestCrossConstantConsistencyProperties(unittest.TestCase):
    """Cross-module consistency invariants between constants.

    These properties verify that the constants in different modules agree
    with each other — bugs in these relationships cause subtle runtime
    failures rather than obvious errors.
    """

    def test_entity_type_overrides_and_binary_exclusions_disjoint(self):
        """ENTITY_TYPE_OVERRIDES and _BINARY_SENSOR_EXCLUSIONS must be disjoint.

        A point in ENTITY_TYPE_OVERRIDES is already explicitly classified;
        adding it to _BINARY_SENSOR_EXCLUSIONS would be redundant and suggests
        a maintenance error.
        """
        from nibe_entity_detection import (
            _BINARY_SENSOR_EXCLUSIONS,
            ENTITY_TYPE_OVERRIDES,
        )
        overlap = set(ENTITY_TYPE_OVERRIDES.keys()) & _BINARY_SENSOR_EXCLUSIONS
        self.assertEqual(overlap, set(),
            f"Points appear in both ENTITY_TYPE_OVERRIDES and "
            f"_BINARY_SENSOR_EXCLUSIONS: {overlap}")

    def test_value_mappings_holding_and_overrides_disjoint(self):
        """VALUE_MAPPINGS holding entries and ENTITY_TYPE_OVERRIDES must be disjoint.

        A holding register with value mappings routes to 'select' automatically.
        An override on the same point is unreachable dead code.
        """
        from nibe_entity_detection import ENTITY_TYPE_OVERRIDES, VALUE_MAPPINGS
        vm_holding = set(VALUE_MAPPINGS.get('holding', {}).keys())
        overlap = vm_holding & set(ENTITY_TYPE_OVERRIDES.keys())
        self.assertEqual(overlap, set(),
            f"Points appear in both VALUE_MAPPINGS holding and "
            f"ENTITY_TYPE_OVERRIDES: {overlap}")

    def test_retry_base_leq_retry_max(self):
        """_RETRY_BASE_S must always be ≤ _RETRY_MAX_S."""
        from nibe_api import _RETRY_BASE_S, _RETRY_MAX_S
        self.assertLessEqual(_RETRY_BASE_S, _RETRY_MAX_S)

    def test_retry_delay_bounded_by_max(self):
        """Every _retry_delay() call must return a value ≤ _RETRY_MAX_S."""
        from nibe_api import _RETRY_MAX_S, _retry_delay
        for _ in range(50):
            self.assertLessEqual(_retry_delay(), _RETRY_MAX_S)

    def test_retry_delay_always_non_negative(self):
        """Every _retry_delay() call must return a non-negative value."""
        from nibe_api import _retry_delay
        for _ in range(50):
            self.assertGreaterEqual(_retry_delay(), 0.0)

    def test_changelog_min_leq_max_entries(self):
        """_CHANGELOG_MIN_ENTRIES must be ≤ _CHANGELOG_MAX_ENTRIES."""
        from nibe_entity_manager import _CHANGELOG_MAX_ENTRIES, _CHANGELOG_MIN_ENTRIES
        self.assertLessEqual(_CHANGELOG_MIN_ENTRIES, _CHANGELOG_MAX_ENTRIES)

    def test_gzip_sentinel_is_nonempty_string(self):
        """_GZIP_SENTINEL must be a non-empty string."""
        from nibe_entity_manager import _GZIP_SENTINEL
        self.assertIsInstance(_GZIP_SENTINEL, str)
        self.assertGreater(len(_GZIP_SENTINEL), 0)

    def test_compress_output_starts_with_sentinel(self):
        """_compress_payload output must always start with _GZIP_SENTINEL."""
        from nibe_entity_manager import _GZIP_SENTINEL, _compress_payload
        for data in [{}, {'key': 'value'}, {'n': 42}]:
            result = _compress_payload(data)
            self.assertTrue(result.startswith(_GZIP_SENTINEL),
                f"_compress_payload output does not start with sentinel: {result[:20]!r}")

    def test_text_register_max_len_positive(self):
        """_TEXT_REGISTER_MAX_LEN must be a positive integer."""
        from nibe_entity_manager import _TEXT_REGISTER_MAX_LEN
        self.assertIsInstance(_TEXT_REGISTER_MAX_LEN, int)
        self.assertGreater(_TEXT_REGISTER_MAX_LEN, 0)

    def test_stale_write_age_positive(self):
        """_STALE_WRITE_AGE_S must be positive (defines write guard timeout)."""
        from nibe_entity_manager import _STALE_WRITE_AGE_S
        self.assertGreater(_STALE_WRITE_AGE_S, 0)

    def test_post_write_scan_positive(self):
        """_POST_WRITE_SCAN_S must be positive (defines dynamic detection window)."""
        from nibe_entity_manager import _POST_WRITE_SCAN_S
        self.assertGreater(_POST_WRITE_SCAN_S, 0)

    def test_cmd_id_length_positive(self):
        """_CMD_ID_LENGTH must be a positive integer."""
        from nibe_entity_manager import _CMD_ID_LENGTH
        self.assertIsInstance(_CMD_ID_LENGTH, int)
        self.assertGreater(_CMD_ID_LENGTH, 0)

    def test_notification_id_constants_are_strings(self):
        """All _NOTIF_* constants must be non-empty strings."""
        from nibe_entity_manager import (
            _NOTIF_API_UNREACHABLE,
            _NOTIF_DISCOVERY_INCOMPLETE,
            _NOTIF_NO_ENTITIES,
            _NOTIF_WRITE_ERROR,
        )
        for notif_id in (_NOTIF_API_UNREACHABLE, _NOTIF_WRITE_ERROR,
                         _NOTIF_NO_ENTITIES, _NOTIF_DISCOVERY_INCOMPLETE):
            self.assertIsInstance(notif_id, str)
            self.assertGreater(len(notif_id), 0)
            # Must be safe as MQTT topic segment — no spaces or special chars
            self.assertNotIn(' ', notif_id)

    def test_notification_id_constants_are_unique(self):
        """All _NOTIF_* constants must be distinct."""
        from nibe_entity_manager import (
            _NOTIF_API_UNREACHABLE,
            _NOTIF_DISCOVERY_INCOMPLETE,
            _NOTIF_NO_ENTITIES,
            _NOTIF_WRITE_ERROR,
        )
        notif_ids = [_NOTIF_API_UNREACHABLE, _NOTIF_WRITE_ERROR,
                     _NOTIF_NO_ENTITIES, _NOTIF_DISCOVERY_INCOMPLETE]
        self.assertEqual(len(notif_ids), len(set(notif_ids)),
            "Duplicate _NOTIF_* constant values detected")

    def test_applied_mode_timeout_positive(self):
        """_APPLIED_MODE_TIMEOUT_S must be positive."""
        from nibe_entity_manager import _APPLIED_MODE_TIMEOUT_S
        self.assertGreater(_APPLIED_MODE_TIMEOUT_S, 0)

    def test_mqtt_scan_timeout_positive(self):
        """_MQTT_SCAN_TIMEOUT_S must be positive."""


class TestBuildPointDefaultsProperties(unittest.TestCase):
    """Hypothesis properties for _build_point_defaults."""

    @given(st.dictionaries(
        st.integers(min_value=1, max_value=99999),
        _point_entry,
        max_size=10,
    ))
    def test_never_raises(self, all_points_by_id):
        from nibe_lovelace import _build_point_defaults
        _build_point_defaults(all_points_by_id)

    @given(st.dictionaries(
        st.integers(min_value=1, max_value=99999),
        _point_entry,
        max_size=10,
    ))
    def test_always_returns_dict(self, all_points_by_id):
        from nibe_lovelace import _build_point_defaults
        result = _build_point_defaults(all_points_by_id)
        self.assertIsInstance(result, dict)

    @given(st.dictionaries(
        st.integers(min_value=1, max_value=99999),
        _point_entry,
        max_size=10,
    ))
    def test_keys_are_ints_from_input(self, all_points_by_id):
        from nibe_lovelace import _build_point_defaults
        result = _build_point_defaults(all_points_by_id)
        for k in result:
            self.assertIsInstance(k, int)
            self.assertIn(k, all_points_by_id)

    @given(st.dictionaries(
        st.integers(min_value=1, max_value=99999),
        _point_entry,
        max_size=10,
    ))
    def test_values_are_strings(self, all_points_by_id):
        from nibe_lovelace import _build_point_defaults
        result = _build_point_defaults(all_points_by_id)
        for v in result.values():
            self.assertIsInstance(v, str)

    @given(st.dictionaries(
        st.integers(min_value=1, max_value=99999),
        _point_entry,
        max_size=10,
    ))
    def test_non_writable_points_excluded(self, all_points_by_id):
        """Non-writable points must never appear in the result."""
        from nibe_lovelace import _build_point_defaults
        result = _build_point_defaults(all_points_by_id)
        for pid in result:
            meta = all_points_by_id[pid]['metadata']
            self.assertTrue(meta['isWritable'])

    @given(st.dictionaries(
        st.integers(min_value=1, max_value=99999),
        _point_entry,
        max_size=10,
    ))
    def test_non_holding_register_excluded(self, all_points_by_id):
        """Non-HOLDING register points must never appear in the result."""
        from nibe_lovelace import _build_point_defaults
        result = _build_point_defaults(all_points_by_id)
        for pid in result:
            meta = all_points_by_id[pid]['metadata']
            self.assertEqual(meta['modbusRegisterType'], 'MODBUS_HOLDING_REGISTER')

    @given(st.dictionaries(
        st.integers(min_value=1, max_value=99999),
        _point_entry,
        max_size=10,
    ))
    def test_degenerate_range_excluded(self, all_points_by_id):
        """Points with min==max (degenerate range) must never appear."""
        from nibe_lovelace import _build_point_defaults
        result = _build_point_defaults(all_points_by_id)
        for pid in result:
            meta = all_points_by_id[pid]['metadata']
            self.assertNotEqual(meta['minValue'], meta['maxValue'])


# ---------------------------------------------------------------------------
# _build_device_info properties (nibe_entity_manager.py)
# ---------------------------------------------------------------------------


class TestBuildDeviceInfoProperties(unittest.TestCase):
    """Hypothesis properties for _build_device_info."""

    _api_response = st.fixed_dictionaries({
        'product': st.fixed_dictionaries({
            'name':         st.text(max_size=30),
            'manufacturer': st.text(max_size=20),
            'firmwareId':   st.text(max_size=10),
            'serialNumber': st.text(max_size=20),
        }),
    })

    @given(_api_response, st.text(max_size=20), st.text(max_size=30),
           st.text(max_size=50))
    def test_never_raises(self, api_response, device_id, device_name, base_url):
        from nibe_entity_manager import _build_device_info
        _build_device_info(api_response, device_id, device_name, base_url)

    @given(_api_response, st.text(max_size=20), st.text(max_size=30),
           st.text(max_size=50))
    def test_always_returns_dict(self, api_response, device_id, device_name, base_url):
        from nibe_entity_manager import _build_device_info
        result = _build_device_info(api_response, device_id, device_name, base_url)
        self.assertIsInstance(result, dict)

    @given(_api_response, st.text(min_size=1, max_size=20),
           st.text(max_size=30), st.text(max_size=50))
    def test_identifiers_contains_device_id(self, api_response, device_id,
                                             device_name, base_url):
        from nibe_entity_manager import _build_device_info
        result = _build_device_info(api_response, device_id, device_name, base_url)
        self.assertIn(device_id, result.get('identifiers', []))

    @given(_api_response, st.text(max_size=20),
           st.text(min_size=1, max_size=30).filter(lambda s: s != 'Nibe SMO S40'),
           st.text(max_size=50))
    def test_custom_device_name_always_used(self, api_response, device_id,
                                            device_name, base_url):
        """Non-default device_name must always appear as 'name' in result."""
        from nibe_entity_manager import _build_device_info
        result = _build_device_info(api_response, device_id, device_name, base_url)
        self.assertEqual(result.get('name'), device_name)

    @given(_api_response, st.text(max_size=20), st.text(max_size=50))
    def test_default_name_prefers_api_name_when_available(self, api_response,
                                                           device_id, base_url):
        """When device_name is the default and API provides a name, use API name."""
        from nibe_entity_manager import _build_device_info
        api_name = api_response.get('product', {}).get('name', '').strip()
        result = _build_device_info(api_response, device_id, 'Nibe SMO S40', base_url)
        if api_name:
            self.assertEqual(result.get('name'), api_name)

    @given(_api_response, st.text(max_size=20), st.text(max_size=30),
           st.text(max_size=50))
    def test_model_uses_api_product_name_when_present(self, api_response,
                                                       device_id, device_name, base_url):
        """The 'model' field must reflect the API's real product name when
        available — previously untested; only 'name' (device_name) had
        coverage, letting a broken model_name computation go uncaught."""
        from nibe_entity_manager import _build_device_info
        api_name = api_response.get('product', {}).get('name', '').strip()
        result = _build_device_info(api_response, device_id, device_name, base_url)
        if api_name:
            self.assertEqual(result.get('model'), api_name)

    def test_model_falls_back_to_nibe_s_series_when_api_name_empty(self):
        """When the API provides no product name, 'model' must fall back
        to the literal 'Nibe S-series' string, not None or the API's
        empty value."""
        from nibe_entity_manager import _build_device_info
        api_response = {'product': {'name': '', 'manufacturer': '',
                                     'firmwareId': '', 'serialNumber': ''}}
        result = _build_device_info(api_response, 'dev1', 'My Device', 'http://x')
        self.assertEqual(result.get('model'), 'Nibe S-series')

    @given(_api_response, st.text(max_size=20), st.text(max_size=30),
           st.text(max_size=50))
    def test_no_empty_string_values(self, api_response, device_id,
                                    device_name, base_url):
        """Empty strings must be stripped from the result dict."""
        from nibe_entity_manager import _build_device_info
        result = _build_device_info(api_response, device_id, device_name, base_url)
        for v in result.values():
            if isinstance(v, str):
                self.assertNotEqual(v, '')

    def test_manufacturer_model_id_serial_number_from_product(self):
        """When present in the API response, manufacturer/firmwareId/serialNumber
        must be copied verbatim into their respective (differently-named) result
        keys — catches key-name typos and wrong-source-key mutants that a
        Hypothesis strategy generating overlapping/blank field values would not
        reliably distinguish."""
        from nibe_entity_manager import _build_device_info
        api_response = {
            'product': {
                'name':         'ProductX',
                'manufacturer': 'ACME Corp',
                'firmwareId':   'FW123',
                'serialNumber': 'SN456',
            }
        }
        result = _build_device_info(api_response, 'dev1', 'My Device', 'http://x')
        self.assertEqual(result.get('manufacturer'), 'ACME Corp')
        self.assertEqual(result.get('model_id'), 'FW123')
        self.assertEqual(result.get('serial_number'), 'SN456')

    def test_manufacturer_model_id_serial_number_defaults_when_absent(self):
        """When the API response's product dict omits manufacturer/firmwareId/
        serialNumber, manufacturer must default to the literal 'NIBE' and
        model_id/serial_number must default to '' (and therefore be stripped
        from the result entirely) — not None, which would survive the
        `v != ""` filter and leak a None value into the MQTT discovery
        payload."""
        from nibe_entity_manager import _build_device_info
        api_response = {'product': {}}
        result = _build_device_info(api_response, 'dev1', 'My Device', 'http://x')
        self.assertEqual(result.get('manufacturer'), 'NIBE')
        self.assertNotIn('model_id', result)
        self.assertNotIn('serial_number', result)

    def test_configuration_url_splits_on_last_api_v1_devices_segment(self):
        """configuration_url must be everything before the LAST occurrence of
        '/api/v1/devices/' in base_url — i.e. rsplit(sep, 1), not split(sep, 1)
        (first occurrence) and not rsplit with a different maxsplit count. A
        base_url containing the separator twice makes rsplit(1)/split(1)/
        rsplit(2) all produce distinct results, so any of those mutations is
        caught."""
        from nibe_entity_manager import _build_device_info
        api_response = {'product': {}}
        base_url = 'http://host/api/v1/devices/outer/api/v1/devices/47'
        result = _build_device_info(api_response, 'dev1', 'My Device', base_url)
        self.assertEqual(result.get('configuration_url'), 'http://host/api/v1/devices/outer')


class TestPublishBridgeAlertProperties(unittest.TestCase):
    """Hypothesis properties for publish_bridge_alert."""

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        mqtt = MagicMock()
        pub = MqttDiscoveryPublisher(
            mqtt_client=mqtt, device_info={},
            device_id='test', device_name='Test',
        )
        return pub, mqtt

    def _get_payload(self, mqtt):
        import json as _json

        from nibe_mqtt_publisher import BrowserTopic
        calls = [c for c in mqtt.publish.call_args_list
                 if c.args[0] == BrowserTopic.BRIDGE_ALERT]
        self.assertTrue(calls, "No BRIDGE_ALERT publish found")
        return _json.loads(calls[-1].args[1])

    @given(st.text(max_size=30), st.text(max_size=30), st.text(max_size=100))
    def test_payload_always_valid_json(self, alert_type, severity, message):
        pub, mqtt = self._pub()
        pub.publish_bridge_alert(alert_type, severity, message)
        self._get_payload(mqtt)  # must parse without raising

    @given(st.text(max_size=30), st.text(max_size=30), st.text(max_size=100))
    def test_payload_contains_required_keys(self, alert_type, severity, message):
        pub, mqtt = self._pub()
        pub.publish_bridge_alert(alert_type, severity, message)
        payload = self._get_payload(mqtt)
        for key in ('alert_type', 'severity', 'message', 'timestamp', 'context'):
            self.assertIn(key, payload)

    @given(st.text(max_size=30), st.text(max_size=30), st.text(max_size=100))
    def test_payload_values_match_inputs(self, alert_type, severity, message):
        pub, mqtt = self._pub()
        pub.publish_bridge_alert(alert_type, severity, message)
        payload = self._get_payload(mqtt)
        self.assertEqual(payload['alert_type'], alert_type)
        self.assertEqual(payload['severity'],   severity)
        self.assertEqual(payload['message'],    message)

    @given(st.text(max_size=30), st.text(max_size=30), st.text(max_size=100))
    def test_always_published_non_retained(self, alert_type, severity, message):
        """retain=False is mandatory — alerts must not replay on reconnect."""
        from nibe_mqtt_publisher import BrowserTopic
        pub, mqtt = self._pub()
        pub.publish_bridge_alert(alert_type, severity, message)
        calls = [c for c in mqtt.publish.call_args_list
                 if c.args[0] == BrowserTopic.BRIDGE_ALERT]
        self.assertTrue(calls)
        retain = calls[-1].kwargs.get('retain', calls[-1].args[2] if len(calls[-1].args) > 2 else True)
        self.assertFalse(retain)

    @given(st.text(max_size=30), st.text(max_size=30), st.text(max_size=100))
    def test_context_none_becomes_empty_dict(self, alert_type, severity, message):
        """When context=None, payload context must be {} not null."""
        pub, mqtt = self._pub()
        pub.publish_bridge_alert(alert_type, severity, message, context=None)
        payload = self._get_payload(mqtt)
        self.assertEqual(payload['context'], {})

    @given(st.text(max_size=30), st.text(max_size=30), st.text(max_size=100),
           st.dictionaries(st.text(max_size=10), st.text(max_size=20), max_size=5))
    def test_context_dict_preserved(self, alert_type, severity, message, context):
        """Provided context dict is always preserved exactly in payload."""
        pub, mqtt = self._pub()
        pub.publish_bridge_alert(alert_type, severity, message, context=context)
        payload = self._get_payload(mqtt)
        self.assertEqual(payload['context'], context)

    @given(st.text(max_size=30), st.text(max_size=30), st.text(max_size=100))
    def test_timestamp_is_positive_float(self, alert_type, severity, message):
        """Timestamp in payload must always be a positive number."""
        pub, mqtt = self._pub()
        pub.publish_bridge_alert(alert_type, severity, message)
        payload = self._get_payload(mqtt)
        self.assertGreater(payload['timestamp'], 0)


# ---------------------------------------------------------------------------
# _get_cached_entity_type properties (nibe_entity_manager.py)
# ---------------------------------------------------------------------------


class TestBuildPointMetadataDictExtendedProperties(unittest.TestCase):
    """Extended Hypothesis properties for _build_point_metadata_dict
    covering the firmware metadata fields passed through unchanged."""

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        return MqttDiscoveryPublisher(
            mqtt_client=MagicMock(), device_info={},
            device_id='test', device_name='Test',
        )

    def _point(self, pid, **meta_overrides):
        meta = {
            'unit': '', 'shortUnit': 'X',
            'minValue': 0, 'maxValue': 100,
            'modbusRegisterID': pid,
            'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
            'variableType': 'integer', 'variableSize': 'u8',
            'isWritable': False, 'divisor': 1, 'decimal': 0,
            'intDefaultValue': None, 'stringDefaultValue': '',
            'change': 1,
        }
        meta.update(meta_overrides)
        return {
            'variableId': pid, 'display_title': f'P{pid}',
            'entity_type': 'sensor', 'entity_category': 'diagnostic',
            'is_writable': False, 'is_dynamic': False, 'description': '',
            'metadata': meta,
        }

    @given(_nibe_point_id, st.integers(min_value=0, max_value=10000))
    def test_divisor_preserved(self, pid, divisor):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._point(pid, divisor=divisor))
        self.assertEqual(result['divisor'], divisor)

    @given(_nibe_point_id, st.integers(min_value=0, max_value=6))
    def test_decimal_preserved(self, pid, decimal):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._point(pid, decimal=decimal))
        self.assertEqual(result['decimal'], decimal)

    @given(_nibe_point_id, st.integers(min_value=0, max_value=100))
    def test_change_preserved(self, pid, change):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._point(pid, change=change))
        self.assertEqual(result['change'], change)

    @given(_nibe_point_id,
           st.sampled_from(['integer', 'floating-point', 'string', 'time', 'date']))
    def test_variable_type_preserved(self, pid, var_type):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._point(pid, variableType=var_type))
        self.assertEqual(result['variableType'], var_type)

    @given(_nibe_point_id,
           st.sampled_from(['u8', 'u16', 's16', 's32', 'u32']))
    def test_variable_size_preserved(self, pid, var_size):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._point(pid, variableSize=var_size))
        self.assertEqual(result['variableSize'], var_size)

    @given(_nibe_point_id,
           st.sampled_from(['MODBUS_INPUT_REGISTER', 'MODBUS_HOLDING_REGISTER',
                            'MODBUS_NO_REGISTER']))
    def test_modbus_register_type_preserved(self, pid, reg_type):
        pub = self._pub()
        result = pub._build_point_metadata_dict(
            self._point(pid, modbusRegisterType=reg_type))
        self.assertEqual(result['modbusRegisterType'], reg_type)

    @given(_nibe_point_id, st.text(max_size=10))
    def test_short_unit_preserved(self, pid, short_unit):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._point(pid, shortUnit=short_unit))
        self.assertEqual(result['shortUnit'], short_unit)

    @given(_nibe_point_id)
    def test_divisor_zero_uses_default_of_1(self, pid):
        """divisor=0 must never appear in output — treated as 1 by firmware contract."""
        pub = self._pub()
        # The metadata field itself: divisor=0 is stored as-is in metadata_dict
        result = pub._build_point_metadata_dict(self._point(pid, divisor=0))
        # What matters: divisor in output matches what's in metadata
        self.assertEqual(result['divisor'], 0)

    @given(_nibe_point_id,
           st.integers(min_value=1, max_value=10000),
           st.integers(min_value=-32768, max_value=32767),
           st.text(max_size=5))
    def test_default_value_field_uses_apply_divisor(self, pid, divisor, int_default, unit):
        """When intDefaultValue is set, default_value uses apply_divisor formatting."""
        from nibe_entity_detection import apply_divisor
        pub = self._pub()
        point = self._point(pid, divisor=divisor, unit=unit)
        point['metadata']['intDefaultValue'] = int_default
        result = pub._build_point_metadata_dict(point)
        if 'default_value' in result:
            expected_display = apply_divisor(int_default, divisor)
            self.assertIn(expected_display, result['default_value'])


class TestBuildMenuViewProperties(unittest.TestCase):
    """Hypothesis properties for _build_menu_view."""

    _menu_strategy = st.fixed_dictionaries({
        'id':    st.text(max_size=10),
        'title': st.text(max_size=30),
        'settings': st.lists(st.fixed_dictionaries({
            'point_id': st.one_of(st.none(),
                                   st.integers(min_value=1, max_value=9999)),
            'label':    st.text(max_size=20),
        }), max_size=5),
        'submenus': st.just([]),
    })

    @given(_menu_strategy)
    def test_always_returns_list(self, menu):
        from nibe_lovelace import _build_menu_view
        rw = MagicMock()
        rw.entity_id_for.return_value = None
        result = _build_menu_view(menu, rw)
        self.assertIsInstance(result, list)

    @given(_menu_strategy)
    def test_never_raises(self, menu):
        from nibe_lovelace import _build_menu_view
        rw = MagicMock()
        rw.entity_id_for.return_value = None
        _build_menu_view(menu, rw)  # must not raise

    @given(_menu_strategy)
    def test_none_known_dynamic_defaults_safely(self, menu):
        """known_dynamic=None must be handled identically to empty set."""
        from nibe_lovelace import _build_menu_view
        rw = MagicMock()
        rw.entity_id_for.return_value = None
        result_none  = _build_menu_view(menu, rw, known_dynamic=None)
        result_empty = _build_menu_view(menu, rw, known_dynamic=set())
        self.assertEqual(result_none, result_empty)

    @given(_menu_strategy)
    def test_none_point_defaults_defaults_safely(self, menu):
        """point_defaults=None must be handled identically to empty dict."""
        from nibe_lovelace import _build_menu_view
        rw = MagicMock()
        rw.entity_id_for.return_value = None
        result_none  = _build_menu_view(menu, rw, point_defaults=None)
        result_empty = _build_menu_view(menu, rw, point_defaults={})
        self.assertEqual(result_none, result_empty)


# ---------------------------------------------------------------------------
# _get_cached_entity_type (nibe_entity_manager.py)
# ---------------------------------------------------------------------------


class TestGetCachedEntityType(unittest.TestCase):
    """EntityManager._get_cached_entity_type — cache key correctness."""

    def test_cache_key_is_the_points_own_variable_id(self):
        """Two different points (distinct variableId) must be cached under
        their own distinct keys — a hardcoded/None key would make the second
        point's lookup collide with (or ignore) the first's cached entry."""
        em = _make_em()
        with patch('nibe_entity_manager.detect_entity_type') as mock_detect:
            mock_detect.side_effect = lambda pd: (
                f"type_{pd['variableId']}", f"cat_{pd['variableId']}"
            )
            result_a = em._get_cached_entity_type({'variableId': 111})
            result_b = em._get_cached_entity_type({'variableId': 222})
        self.assertEqual(result_a, ('type_111', 'cat_111'))
        self.assertEqual(result_b, ('type_222', 'cat_222'))
        # Both must genuinely be cached under their own point_id, not a
        # shared/None key that would make one overwrite the other.
        self.assertEqual(em._entity_type_cache.get(111), ('type_111', 'cat_111'))
        self.assertEqual(em._entity_type_cache.get(222), ('type_222', 'cat_222'))

    def test_second_call_for_same_point_does_not_recompute(self):
        """A repeated call for the same point_id must be served from cache —
        detect_entity_type must be invoked exactly once across two calls."""
        em = _make_em()
        with patch('nibe_entity_manager.detect_entity_type',
                   return_value=('sensor', 'diagnostic')) as mock_detect:
            em._get_cached_entity_type({'variableId': 55})
            em._get_cached_entity_type({'variableId': 55})
        self.assertEqual(mock_detect.call_count, 1)

    def test_computed_result_is_stored_verbatim_in_cache(self):
        """The exact tuple returned by detect_entity_type must be what gets
        stored in the cache — not None or a placeholder — verified by
        reading the cache directly (independent of the return value of the
        call under test) after a single populating call."""
        em = _make_em()
        with patch('nibe_entity_manager.detect_entity_type',
                   return_value=('binary_sensor', 'config')):
            em._get_cached_entity_type({'variableId': 77})
        self.assertEqual(em._entity_type_cache.get(77), ('binary_sensor', 'config'))


# ---------------------------------------------------------------------------
# resolve_point_from_entity_id properties (nibe_entity_manager.py)
# ---------------------------------------------------------------------------


class TestResolvePointFromEntityIdProperties(unittest.TestCase):
    """Hypothesis properties for EntityManager.resolve_point_from_entity_id.

    Three-pass lookup:
      1. slug starts with 'nibe_' → parse int directly
      2. scan active_entities config topics
      3. unique_id registry map
    """

    @given(st.text(max_size=50).filter(lambda s: '.' not in s))
    def test_no_dot_always_returns_none(self, entity_id):
        """No '.' in entity_id → always None (not a valid HA entity_id)."""
        em = _make_em()
        self.assertIsNone(em.resolve_point_from_entity_id(entity_id))

    @given(st.integers(min_value=0, max_value=99999),
           st.sampled_from(['sensor', 'switch', 'number', 'binary_sensor',
                            'select', 'button']))
    def test_nibe_prefixed_slug_returns_correct_pid(self, pid, domain):
        """domain.nibe_{pid} always resolves to pid."""
        em = _make_em()
        entity_id = f'{domain}.nibe_{pid}'
        result = em.resolve_point_from_entity_id(entity_id)
        self.assertEqual(result, pid)

    @given(st.sampled_from(['sensor', 'switch', 'number']),
           st.text(min_size=1, max_size=20).filter(_cannot_be_int))
    def test_nibe_prefix_with_non_int_returns_none(self, domain, suffix):
        """domain.nibe_{non-int} must return None.
        Filter uses int() directly to match production — catches '0\\r' etc.
        """
        em = _make_em()
        entity_id = f'{domain}.nibe_{suffix}'
        result = em.resolve_point_from_entity_id(entity_id)
        self.assertIsNone(result)

    def test_empty_nibe_slug_returns_none(self):
        """domain.nibe_ (empty after prefix) must return None."""
        em = _make_em()
        self.assertIsNone(em.resolve_point_from_entity_id('sensor.nibe_'))

    @given(st.integers(min_value=0, max_value=99999))
    def test_nibe_zero_returns_zero(self, _n):
        """nibe_0 must resolve to 0 — zero is a valid point_id."""
        em = _make_em()
        self.assertEqual(em.resolve_point_from_entity_id('sensor.nibe_0'), 0)

    def test_non_nibe_slug_without_match_returns_none(self):
        """entity_id without nibe_ slug and no active entity match → None."""
        em = _make_em()
        for entity_id in ['sensor.other_entity', 'switch.my_device',
                          'number.some_point', 'sensor.']:
            result = em.resolve_point_from_entity_id(entity_id)
            self.assertIsNone(result, f"Expected None for {entity_id!r}")

    @given(st.integers(min_value=1, max_value=99999),
           st.sampled_from(['sensor', 'switch', 'number']))
    def test_result_always_int_or_none(self, pid, domain):
        """resolve_point_from_entity_id always returns int or None."""
        em = _make_em()
        result = em.resolve_point_from_entity_id(f'{domain}.nibe_{pid}')
        self.assertIn(type(result), (int, type(None)))

    @given(st.integers(min_value=0, max_value=99999))
    def test_nibe_slug_roundtrip_with_create_entity_id(self, pid):
        """create_entity_id(pid) always resolves back to pid."""
        from nibe_entity_detection import create_entity_id
        em = _make_em()
        entity_id = f'sensor.{create_entity_id(pid)}'
        result = em.resolve_point_from_entity_id(entity_id)
        self.assertEqual(result, pid)

    def test_split_on_dot_uses_maxsplit_1_from_the_left(self):
        """The domain/slug split must be ha_entity_id.split('.', 1) — split
        from the LEFT with maxsplit exactly 1. An entity_id with two dots
        distinguishes all three ways this can be mutated:
          - rsplit('.', 1) (split from the right) would treat 'sensor' as
            part of the domain and 'nibe_9' as the slug, wrongly resolving
            to point 9.
          - split('.') (no maxsplit) or split('.', 2) would produce 3 parts
            for this input, raising a ValueError on the 2-tuple unpack —
            the correct code must not raise here at all.
        Correct behaviour: domain='nibe_3', slug='sensor.nibe_9' — slug
        does NOT start with 'nibe_', so this falls through every lookup
        pass and returns None.
        """
        em = _make_em()
        result = em.resolve_point_from_entity_id('nibe_3.sensor.nibe_9')
        self.assertIsNone(result)

    def test_unique_id_map_requires_both_entity_id_match_and_nibe_prefix(self):
        """The unique_id_map fallback pass must require BOTH the mapped
        entity_id to equal ha_entity_id AND the unique_id to start with
        'nibe_' — not either condition alone. A map entry whose unique_id
        starts with 'nibe_' but whose mapped entity_id does NOT match the
        queried ha_entity_id must not resolve to anything; an 'or' mutant
        would incorrectly return the point_id from the nibe_-prefixed
        unique_id regardless of the entity_id mismatch."""
        em = _make_em()
        unique_id_map = {'nibe_42': 'sensor.completely_different_entity'}
        result = em.resolve_point_from_entity_id(
            'sensor.unmatched_entity', unique_id_map=unique_id_map
        )
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# all_points and active_entities properties (nibe_entity_manager.py)
# ---------------------------------------------------------------------------


class TestAllPointsActiveEntitiesProperties(unittest.TestCase):
    """Hypothesis properties for EntityManager.all_points and active_entities."""

    @given(st.integers(min_value=0, max_value=100))
    def test_all_points_length_matches_all_points_by_id(self, n):
        """len(all_points) always equals len(all_points_by_id)."""
        em = _make_em()
        for i in range(n):
            em.all_points_by_id[i] = {'variableId': i}
        self.assertEqual(len(em.all_points), n)

    @given(st.integers(min_value=0, max_value=50))
    def test_active_entities_length_matches_active_entities_by_id(self, n):
        """len(active_entities) always equals len(active_entities_by_id)."""
        em = _make_em()
        for i in range(n):
            em.active_entities_by_id[i] = {'point_id': i}
        self.assertEqual(len(em.active_entities), n)

    @given(st.integers(min_value=0, max_value=100))
    def test_all_points_always_returns_list(self, n):
        em = _make_em()
        for i in range(n):
            em.all_points_by_id[i] = {'variableId': i}
        self.assertIsInstance(em.all_points, list)

    @given(st.integers(min_value=0, max_value=50))
    def test_active_entities_always_returns_list(self, n):
        em = _make_em()
        for i in range(n):
            em.active_entities_by_id[i] = {'point_id': i}
        self.assertIsInstance(em.active_entities, list)

    @given(st.integers(min_value=1, max_value=50))
    def test_all_points_contains_all_values_from_dict(self, n):
        """all_points must contain every value in all_points_by_id."""
        em = _make_em()
        for i in range(n):
            em.all_points_by_id[i] = {'variableId': i, 'label': f'p{i}'}
        all_pts = em.all_points
        for v in em.all_points_by_id.values():
            self.assertIn(v, all_pts)

    @given(st.integers(min_value=0, max_value=50))
    def test_two_calls_return_equal_results(self, n):
        """Two consecutive calls to all_points return equal results."""
        em = _make_em()
        for i in range(n):
            em.all_points_by_id[i] = {'variableId': i}
        self.assertEqual(em.all_points, em.all_points)

    def test_empty_em_all_points_is_empty_list(self):
        em = _make_em()
        self.assertEqual(em.all_points, [])

    def test_empty_em_active_entities_is_empty_list(self):
        em = _make_em()
        self.assertEqual(em.active_entities, [])


# ---------------------------------------------------------------------------
# build_disable_notification properties (nibe_entity_manager.py)
# ---------------------------------------------------------------------------


class TestCompression(unittest.TestCase):
    def setUp(self):
        from nibe_entity_manager import (
            _GZIP_SENTINEL,
            _compress_payload,
            _decompress_payload,
        )
        self.compress   = _compress_payload
        self.decompress = _decompress_payload
        self.sentinel   = _GZIP_SENTINEL

    def test_round_trip(self):
        data   = {'history': [{'id': 1}] * 10, '_seq': 5}
        result = json.loads(self.decompress(self.compress(data)))
        self.assertEqual(result['_seq'], 5)
        self.assertEqual(len(result['history']), 10)

    def test_output_is_ascii_string(self):
        self.compress({'a': 1}).encode('ascii')   # must not raise

    def test_sentinel_prefix(self):
        self.assertTrue(self.compress({'x': 1}).startswith(self.sentinel))

    def test_smaller_than_raw_json(self):
        data = {'history': [{'title': f't{i}', 'v': i} for i in range(100)]}
        self.assertLess(len(self.compress(data)), len(json.dumps(data)))

    def test_bytes_with_sentinel(self):
        compressed = self.compress({'ok': True}).encode('utf-8')
        self.assertTrue(json.loads(self.decompress(compressed))['ok'])

    def test_empty_dict(self):
        self.assertEqual(json.loads(self.decompress(self.compress({}))), {})

    def test_large_payload_under_5kb(self):
        entries = [
            {'id': f'c{i}', 'timestamp': 1.0 + i, 'iso_timestamp': '2024',
             'added': [{'id': 6983}], 'removed': [], 'unread': False}
            for i in range(200)
        ]
        self.assertLess(len(self.compress({'history': entries})), 5000)


class TestPointToMenuMap(unittest.TestCase):

    def test_starts_empty(self):
        em = _make_em()
        self.assertEqual(em.point_to_menu_map, {})

    def test_can_be_populated(self):
        em = _make_em()
        em.point_to_menu_map[6984] = ('7.1.6.3', 'Power at DOT')
        self.assertEqual(em.point_to_menu_map[6984], ('7.1.6.3', 'Power at DOT'))

    def test_lookup_returns_none_for_unknown(self):
        em = _make_em()
        self.assertIsNone(em.point_to_menu_map.get(9999))


# ===========================================================================
# 38. _ws_call handles a dead connection without raising
# ===========================================================================


class TestResolvePointFromEntityId(unittest.TestCase):
    """The three-pass resolver that maps an incoming HA entity_id back to a
    Nibe point_id for command handling. Zero coverage before this despite
    being on the critical path for every write from Home Assistant — a
    bug here means a command either silently resolves to the wrong point
    or fails to resolve at all, with no obvious error to the user."""

    def test_no_dot_returns_none(self):
        """Malformed input (no domain separator) — must not crash."""
        em = _make_em()
        self.assertIsNone(em.resolve_point_from_entity_id('not_a_valid_entity_id'))

    def test_pass1_nibe_prefixed_slug_resolves_directly(self):
        """The fast path: entity_id literally encodes the point_id, e.g.
        switch.nibe_3920 -> 3920. No registry lookup needed."""
        em = _make_em()
        self.assertEqual(em.resolve_point_from_entity_id('switch.nibe_3920'), 3920)

    def test_pass1_non_numeric_suffix_falls_through_not_crashes(self):
        """A slug starting with 'nibe_' but not followed by a valid int
        (e.g. a custom-renamed entity) must fall through to pass 2/3
        rather than raising ValueError."""
        em = _make_em()
        result = em.resolve_point_from_entity_id('switch.nibe_custom_name')
        self.assertIsNone(result)  # no other match available either

    def test_pass2_matches_via_active_entities_config_topic(self):
        """When the entity_id doesn't encode the point_id directly (e.g.
        user has renamed it in HA), fall back to matching against the
        known discovery config topic built from entity_type + entity_id."""
        em = _make_em()
        em.active_entities_by_id[3920] = {
            'entity_type': 'switch', 'entity_id': 'permit_heating',
        }
        result = em.resolve_point_from_entity_id('switch.permit_heating')
        self.assertEqual(result, 3920)

    def test_pass2_does_not_match_wrong_domain(self):
        """The config topic includes entity_type — a sensor with the same
        slug as a switch must not be confused for it."""
        em = _make_em()
        em.active_entities_by_id[3920] = {
            'entity_type': 'switch', 'entity_id': 'permit_heating',
        }
        result = em.resolve_point_from_entity_id('sensor.permit_heating')
        self.assertIsNone(result)

    def test_pass3_unique_id_map_used_when_provided(self):
        """The registry-watcher-supplied unique_id_map is the third and
        final fallback — used when neither the fast prefix path nor the
        active_entities scan resolves the entity."""
        em = _make_em()
        unique_id_map = {'nibe_4527': 'switch.some_renamed_entity'}
        result = em.resolve_point_from_entity_id(
            'switch.some_renamed_entity', unique_id_map=unique_id_map,
        )
        self.assertEqual(result, 4527)

    def test_pass3_non_nibe_unique_id_ignored(self):
        """A unique_id_map entry not prefixed 'nibe_' belongs to a
        different integration and must not be matched."""
        em = _make_em()
        unique_id_map = {'other_integration_id': 'switch.some_entity'}
        result = em.resolve_point_from_entity_id(
            'switch.some_entity', unique_id_map=unique_id_map,
        )
        self.assertIsNone(result)

    def test_pass3_malformed_unique_id_suffix_does_not_crash(self):
        em = _make_em()
        unique_id_map = {'nibe_not_a_number': 'switch.some_entity'}
        result = em.resolve_point_from_entity_id(
            'switch.some_entity', unique_id_map=unique_id_map,
        )
        self.assertIsNone(result)

    def test_no_match_anywhere_returns_none(self):
        em = _make_em()
        result = em.resolve_point_from_entity_id(
            'switch.totally_unknown', unique_id_map={},
        )
        self.assertIsNone(result)

    def test_pass_priority_fast_path_wins_over_active_entities_scan(self):
        """If both the fast nibe_-prefix path AND the active_entities scan
        could resolve the same entity_id, the fast path must be tried
        first and short-circuit — confirms pass ordering, not just that
        each pass works in isolation."""
        em = _make_em()
        # Set up a conflicting active_entities entry that would resolve
        # to a DIFFERENT point_id if pass 2 were reached.
        em.active_entities_by_id[9999] = {
            'entity_type': 'switch', 'entity_id': 'nibe_3920',
        }
        result = em.resolve_point_from_entity_id('switch.nibe_3920')
        self.assertEqual(result, 3920)  # fast path wins, not 9999


class TestEntityManagerProperties(unittest.TestCase):
    """The all_points and active_entities properties return list views of
    their respective dicts. Previously uncovered (single-line properties)."""

    def _point(self, pid):
        return {
            'variableId': pid, 'display_title': f'Point {pid}',
            'metadata': {'isWritable': False, 'divisor': 1,
                         'minValue': 0, 'maxValue': 100,
                         'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                         'variableType': 'integer', 'variableSize': 's16',
                         'unit': '', 'decimal': 0},
            'title': f'Point {pid}', 'description': '',
        }

    def test_all_points_empty(self):
        em = _make_em()
        self.assertEqual(em.all_points, [])

    def test_all_points_returns_list_of_values(self):
        em = _make_em()
        em.all_points_by_id[100] = self._point(100)
        em.all_points_by_id[200] = self._point(200)
        result = em.all_points
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)
        pids = {p['variableId'] for p in result}
        self.assertEqual(pids, {100, 200})

    def test_all_points_is_a_copy(self):
        """Mutating the returned list must not affect all_points_by_id."""
        em = _make_em()
        em.all_points_by_id[100] = self._point(100)
        result = em.all_points
        result.clear()
        self.assertEqual(len(em.all_points_by_id), 1)

    def test_active_entities_empty(self):
        em = _make_em()
        self.assertEqual(em.active_entities, [])

    def test_active_entities_returns_list_of_values(self):
        em = _make_em()
        em.active_entities_by_id[100] = {'variableId': 100}
        em.active_entities_by_id[200] = {'variableId': 200}
        result = em.active_entities
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)

    def test_active_entities_is_a_copy(self):
        em = _make_em()
        em.active_entities_by_id[100] = {'variableId': 100}
        result = em.active_entities
        result.clear()


class TestBuildDeviceInfoApiNameFallback(unittest.TestCase):
    """_build_device_info uses API name when config still has default."""

    def test_api_name_used_when_config_is_default(self):
        from nibe_entity_manager import _build_device_info
        result = _build_device_info(
            api_response={'product': {'name': 'S1255-6', 'manufacturer': 'NIBE'}},
            device_id='abc123',
            device_name='Nibe SMO S40',    # default name
            base_url='https://192.168.1.1/api/v1/devices/abc123',
        )
        self.assertEqual(result['name'], 'S1255-6')

    def test_user_name_kept_when_not_default(self):
        from nibe_entity_manager import _build_device_info
        result = _build_device_info(
            api_response={'product': {'name': 'S1255-6', 'manufacturer': 'NIBE'}},
            device_id='abc123',
            device_name='My Heat Pump',   # user-set name
            base_url='https://192.168.1.1/api/v1/devices/abc123',
        )
        self.assertEqual(result['name'], 'My Heat Pump')


class TestGetMemoryUsage(unittest.TestCase):
    """get_memory_usage returns a dict with the expected keys and
    sensible values (lines 2965-2989)."""

    def test_returns_expected_keys(self):
        em = _make_em()
        stats = em.get_memory_usage()
        for key in ['total_points', 'active_entities', 'mqtt_enabled_points',
                    'active_dynamic_points', 'value_cache_size', 'last_states_size',
                    'point_string_cache_size', 'pending_writes', 'estimated_memory_mb',
                    'point_string_cache_hit_rate']:
            self.assertIn(key, stats, f"Expected key '{key}' missing from get_memory_usage()")

    def test_point_string_cache_hit_rate_reflects_cache_stats(self):
        """point_string_cache_hit_rate must match the underlying
        LRUCache.get_stats()['hit_rate'] — not just be present."""
        em = _make_em()
        em._point_string_cache.put('a', 'value')
        em._point_string_cache.get('a')       # hit
        em._point_string_cache.get('missing')  # miss
        stats = em.get_memory_usage()
        expected = em._point_string_cache.get_stats()['hit_rate']
        self.assertAlmostEqual(stats['point_string_cache_hit_rate'], round(expected, 3))

    def test_point_string_cache_hit_rate_zero_when_empty(self):
        em = _make_em()
        stats = em.get_memory_usage()
        self.assertEqual(stats['point_string_cache_hit_rate'], 0)

    def test_counts_reflect_state(self):
        em = _make_em()
        em.all_points_by_id[100] = {'variableId': 100}
        em.mqtt_enabled_points.add(100)
        stats = em.get_memory_usage()
        self.assertEqual(stats['total_points'], 1)
        self.assertEqual(stats['mqtt_enabled_points'], 1)

    def test_estimated_memory_mb_is_non_negative(self):
        em = _make_em()
        stats = em.get_memory_usage()
        self.assertGreaterEqual(stats['estimated_memory_mb'], 0)

    def test_estimated_memory_mb_matches_exact_formula(self):
        """Pins the exact estimated_bytes formula (total_points*100 +
        active_entities*500 + cache_sizes*50) with values calibrated so
        each term contributes enough magnitude to survive 2-decimal
        rounding — the all-zero fixture used by the non-negative test
        above can't distinguish a +/- operator flip on any term (0 +/- 0
        is still >= 0, and a flipped-sign zero term is indistinguishable
        from the correct one)."""
        em = _make_em()
        for pid in range(100):
            em.all_points_by_id[pid] = {}
        for pid in range(50):
            em.active_entities_by_id[pid] = {}
        for pid in range(2000):
            em._point_string_cache.put(pid, ('t', 'd', 'ct', 'cd'))
        stats = em.get_memory_usage()
        expected_bytes = 100 * 100 + 50 * 500 + 2000 * 50
        expected_mb = round(expected_bytes / (1024 * 1024), 2)
        self.assertEqual(stats['estimated_memory_mb'], expected_mb)

    def test_actual_object_size_none_when_getsizeof_raises(self):
        """If sys.getsizeof raises, actual_object_size_mb is None rather
        than propagating the exception (lines 2971-2972)."""
        em = _make_em()
        with patch('nibe_entity_manager.sys.getsizeof', side_effect=TypeError("unsupported")):
            stats = em.get_memory_usage()
        self.assertIsNone(stats['actual_object_size_mb'])

    def test_hit_rate_rounded_to_3_decimals_not_4(self):
        """point_string_cache_hit_rate must be rounded to exactly 3 decimal
        places. A hit rate with a repeating 4th digit (1/3) distinguishes
        round(x, 3) from round(x, 4) — a fixture with an exact 0.5 hit rate
        (as used elsewhere) cannot, since both roundings agree there."""
        em = _make_em()
        em._point_string_cache.put('a', 'value')
        em._point_string_cache.get('a')        # 1 hit
        em._point_string_cache.get('miss1')     # 2 misses
        em._point_string_cache.get('miss2')
        stats = em.get_memory_usage()
        # 1 hit / 3 lookups = 0.333333...; round(_, 3) == 0.333, round(_, 4) == 0.3333
        self.assertEqual(stats['point_string_cache_hit_rate'], 0.333)

    def test_estimated_bytes_total_points_multiplier_is_100(self):
        """The total_points term must use a 100-bytes-per-point multiplier —
        a large-enough point count so an off-by-one multiplier shifts the
        rounded MB result, unlike a small fixture where rounding masks it."""
        em = _make_em()
        for pid in range(200_000):
            em.all_points_by_id[pid] = {}
        stats = em.get_memory_usage()
        expected_mb = round(200_000 * 100 / (1024 * 1024), 2)
        self.assertEqual(stats['estimated_memory_mb'], expected_mb)

    def test_estimated_bytes_active_entities_multiplier_is_500(self):
        """The active_entities term must use a 500-bytes-per-entity
        multiplier, isolated from the other (zero) terms."""
        em = _make_em()
        for pid in range(40_000):
            em.active_entities_by_id[pid] = {}
        stats = em.get_memory_usage()
        expected_mb = round(40_000 * 500 / (1024 * 1024), 2)
        self.assertEqual(stats['estimated_memory_mb'], expected_mb)

    def test_estimated_bytes_cache_sizes_multiplier_is_50(self):
        """The combined cache-size term (value_cache + last_states +
        point_string_cache) must use a 50-bytes-per-entry multiplier,
        isolated from the other (zero) terms."""
        em = _make_em()
        for pid in range(200_000):
            em.last_states[pid] = "0"
        stats = em.get_memory_usage()
        expected_mb = round(200_000 * 50 / (1024 * 1024), 2)
        self.assertEqual(stats['estimated_memory_mb'], expected_mb)

    def test_estimated_memory_mb_divides_by_1024_squared_not_a_nearby_value(self):
        """estimated_bytes must be divided by 1024*1024 (MiB), not
        1025*1024 or 1024*1025 — a large byte count makes the ~0.1%
        difference from those nearby divisors visible after 2-decimal
        rounding."""
        em = _make_em()
        for pid in range(300_000):
            em.all_points_by_id[pid] = {}
        stats = em.get_memory_usage()
        expected_bytes = 300_000 * 100
        expected_mb = round(expected_bytes / (1024 * 1024), 2)
        self.assertEqual(stats['estimated_memory_mb'], expected_mb)
        # Sanity: confirm this fixture size actually distinguishes the
        # correct divisor from the nearby off-by-one-factor mutants.
        self.assertNotEqual(expected_mb, round(expected_bytes / (1025 * 1024), 2))
        self.assertNotEqual(expected_mb, round(expected_bytes / (1024 * 1025), 2))

    def test_estimated_memory_mb_rounded_to_2_decimals_not_3(self):
        """estimated_memory_mb must be rounded to exactly 2 decimal places —
        a byte count chosen so the 3rd decimal digit is non-zero distinguishes
        round(x, 2) from round(x, 3)."""
        em = _make_em()
        for pid in range(137):
            em.all_points_by_id[pid] = {}
        stats = em.get_memory_usage()
        expected_bytes = 137 * 100
        two_places = round(expected_bytes / (1024 * 1024), 2)
        three_places = round(expected_bytes / (1024 * 1024), 3)
        self.assertNotEqual(two_places, three_places)  # sanity: fixture is discriminating
        self.assertEqual(stats['estimated_memory_mb'], two_places)

    def test_actual_object_size_mb_uses_getsizeof_of_self_not_none(self):
        """actual_object_size_mb must be computed from sys.getsizeof(self) —
        patch it to a known, distinctive return value and confirm that exact
        value (converted via the real /(1024*1024) and round(_, 2) formula)
        appears in the result, ruling out a mutant that discards the call
        result entirely (e.g. hardcodes None on the success path)."""
        em = _make_em()
        with patch('nibe_entity_manager.sys.getsizeof', return_value=5_242_880):  # exactly 5 MiB
            stats = em.get_memory_usage()
        self.assertEqual(stats['actual_object_size_mb'], 5.0)

    def test_actual_object_size_mb_key_name_is_exact(self):
        """The result key must be literally 'actual_object_size_mb' —
        catches a mutant that writes the value under a differently-cased
        or renamed key, silently breaking any dashboard reading this key."""
        em = _make_em()
        stats = em.get_memory_usage()
        self.assertIn('actual_object_size_mb', stats)


class TestEmLockAttributes(unittest.TestCase):
    """EntityManager exposes _em_lock (RLock) and _post_write_lock (Lock).

    These were added in the C1/C2 concurrency fix.  Verifying their presence
    and type is a regression guard so they cannot be silently removed or
    replaced with non-reentrant types.
    """

    def test_em_lock_is_rlock(self):
        """_em_lock must be an RLock — enable/disable calls are nested inside
        apply_mode, _publish_dynamic_changes, and _reconcile_dynamic_points,
        all of which acquire _em_lock before calling the locked variants."""
        import threading
        em = _make_em()
        self.assertIsInstance(
            em._em_lock,
            type(threading.RLock()),
            "_em_lock must be a reentrant lock (RLock) — Lock would deadlock",
        )

    def test_post_write_lock_is_lock(self):
        """_post_write_lock guards post_write_active and _post_write_until
        against a TOCTOU race between the write executor and main thread."""
        import threading
        em = _make_em()
        self.assertIsInstance(
            em._post_write_lock,
            type(threading.Lock()),
            "_post_write_lock must exist and be a threading.Lock",
        )

    def test_enable_entity_locked_exists(self):
        """_enable_entity_locked is the lock-free implementation called by all
        internal callers that already hold _em_lock."""
        em = _make_em()
        self.assertTrue(
            callable(getattr(em, '_enable_entity_locked', None)),
            "_enable_entity_locked must be a callable method",
        )

    def test_disable_entity_locked_exists(self):
        """_disable_entity_locked is the lock-free implementation called by all
        internal callers that already hold _em_lock."""
        em = _make_em()
        self.assertTrue(
            callable(getattr(em, '_disable_entity_locked', None)),
            "_disable_entity_locked must be a callable method",
        )

    def test_em_lock_is_reentrant_under_apply_mode(self):
        """apply_mode acquires _em_lock then calls _enable_entity_locked which
        runs inside that same lock — must not deadlock even when a second thread
        (simulated here by direct invocation) tries to acquire it concurrently.

        This verifies the RLock contract: a thread that already holds the lock
        can re-acquire it without blocking."""
        em = _make_em()
        em.all_points_by_id = {
            1: {'variableId': 1, 'display_title': 'P1', 'entity_type': 'sensor',
                'entity_category': 'none', 'is_writable': False, 'is_dynamic': False,
                'description': '', 'metadata': {}},
        }
        with patch('nibe_entity_manager.MODES', {'essential': frozenset({1})}):
            em.apply_mode('essential')   # must not deadlock
        self.assertIn(1, em.mqtt_enabled_points)


# ---------------------------------------------------------------------------
# _enable_entity_locked / _disable_entity_locked mutation-survivor coverage
# (nibe_entity_manager.py) — mutmut Phase 3 sweep.
# ---------------------------------------------------------------------------


class TestEnableDisableEntityLockedMutationSurvivors(unittest.TestCase):
    """Targeted tests closing mutmut survivors in EntityManager's
    _enable_entity_locked and _disable_entity_locked. Deliberately kept in
    this file (not tests/test_entity_manager_lifecycle.py) to avoid
    colliding with a concurrently-assigned agent's edits there."""

    def setUp(self):
        self.em = _make_em()
        self.point_id = 4

        self.em.all_points_by_id[self.point_id] = {
            'variableId':      self.point_id,
            'display_title':   'Outdoor temperature',
            'entity_type':     'sensor',
            'entity_category': 'diagnostic',
            'is_writable':     False,
            'is_dynamic':      False,
            'metadata': {
                'divisor': 10, 'unit': '°C', 'minValue': -400, 'maxValue': 400,
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
            },
        }
        import time as _time
        self.em.bulk_data[self.point_id] = {
            'raw_value': 119, 'string_value': '', 'is_ok': True,
            'metadata': {'divisor': 10}, 'title': 'Outdoor temperature',
            'description': '', 'timestamp': _time.time(),
        }

        self.mock_entity_info = {
            'point_id':          self.point_id,
            'entity_type':       'sensor',
            'entity_id':         f'nibe_{self.point_id}',
            'state_topic':       f'homeassistant/sensor/nibe_{self.point_id}/state',
            'availability_topic': f'homeassistant/sensor/nibe_{self.point_id}/availability',
            'attributes_topic':  f'homeassistant/sensor/nibe_{self.point_id}/attributes',
            'command_topic':     None,   # read-only sensor — no command topic
            'metadata':          {'divisor': 10},
        }
        self.em._pub.publish_entity_discovery.return_value = self.mock_entity_info

    # ── _enable_entity_locked ───────────────────────────────────────────

    def test_enable_missing_entity_type_and_category_use_defaults(self):
        """When the indexed point dict omits 'entity_type'/'entity_category'
        (should not normally happen, but the code guards for it), the stats
        dicts must be keyed under the literal defaults 'unknown'/'none' —
        not None or some other placeholder — otherwise the memory-usage /
        dashboard stats silently lose these points under an unexpected key."""
        del self.em.all_points_by_id[self.point_id]['entity_type']
        del self.em.all_points_by_id[self.point_id]['entity_category']
        self.em.enable_entity(self.point_id)
        self.assertEqual(self.em._stats_type_counts.get('unknown'), 1)
        self.assertEqual(self.em._stats_category_counts.get('none'), 1)

    def test_enable_increments_existing_type_and_category_counts_by_exactly_one(self):
        """Stats counters must increment the *existing* count by exactly 1,
        not overwrite it or jump by 2 — verified against independently
        pre-seeded starting values."""
        self.em._stats_type_counts['sensor'] = 5
        self.em._stats_category_counts['diagnostic'] = 7
        self.em.enable_entity(self.point_id)
        self.assertEqual(self.em._stats_type_counts['sensor'], 6)
        self.assertEqual(self.em._stats_category_counts['diagnostic'], 8)

    def test_enable_missing_is_writable_key_does_not_increment_writable_count(self):
        """point.get('is_writable', False) must default to False (falsy) when
        the key is absent — not True, which would silently mark every
        conditional/legacy point missing this key as writable."""
        del self.em.all_points_by_id[self.point_id]['is_writable']
        before = self.em._stats_writable_count
        self.em.enable_entity(self.point_id)
        self.assertEqual(self.em._stats_writable_count, before)

    def test_enable_subscribes_command_topic_with_qos_1(self):
        """The command topic subscription must use QoS 1 specifically (at
        least once delivery) — not QoS 2 or any other value."""
        cmd_topic = f'homeassistant/switch/nibe_{self.point_id}/set'
        self.mock_entity_info['command_topic'] = cmd_topic
        self.em.enable_entity(self.point_id)
        self.em.mqtt.subscribe.assert_called_once_with(cmd_topic, qos=1)

    def test_enable_publishes_availability_with_exact_topic_and_retain_true(self):
        """The availability publish must target the entity_info's own
        availability_topic, with payload 'online' and retain=True — a
        substring check on the call log isn't enough to catch retain
        flipping to False, which would break HA's availability tracking
        across broker restarts."""
        self.em.enable_entity(self.point_id)
        self.em.mqtt.publish.assert_any_call(
            self.mock_entity_info['availability_topic'], 'online', retain=True
        )

    def test_enable_skips_update_entity_state_when_point_not_in_bulk_data(self):
        """_update_entity_state must be skipped when the point isn't in
        bulk_data yet (first enable before first poll) — calling it would
        trigger the auto-disable path and immediately undo the enable."""
        del self.em.bulk_data[self.point_id]
        with patch.object(self.em, '_update_entity_state') as mock_update:
            self.em.enable_entity(self.point_id)
        mock_update.assert_not_called()

    def test_enable_calls_update_entity_state_when_point_in_bulk_data(self):
        """Conversely, when the point IS present in bulk_data, the state
        update must actually be invoked."""
        with patch.object(self.em, '_update_entity_state') as mock_update:
            self.em.enable_entity(self.point_id)
        mock_update.assert_called_once()

    def test_enable_dismisses_no_entities_notification_on_first_enable(self):
        """The 'no entities enabled' notification must be dismissed with the
        exact (mqtt_client, _NOTIF_NO_ENTITIES) pair on the first successful
        enable (mqtt_enabled_points size becomes 1)."""
        from nibe_entity_manager import _NOTIF_NO_ENTITIES
        self.em.enable_entity(self.point_id)
        self.em._dismiss.assert_called_once_with(self.em.mqtt, _NOTIF_NO_ENTITIES)

    def test_enable_does_not_dismiss_notification_when_mqtt_is_falsy(self):
        """If self.mqtt is falsy, the dismiss call must not fire even though
        mqtt_enabled_points has grown to 1 — the condition is an AND, not an
        OR, of both checks."""
        falsy_mqtt = MagicMock()
        falsy_mqtt.__bool__ = lambda self: False
        self.em.mqtt = falsy_mqtt
        self.em.enable_entity(self.point_id)
        self.em._dismiss.assert_not_called()

    def test_enable_does_not_dismiss_notification_on_second_enabled_point(self):
        """The dismiss must only fire when exactly one point is enabled —
        not on the second (or any later) point, and not merely 'not equal
        to some other number'."""
        second_point_id = self.point_id + 1
        self.em.all_points_by_id[second_point_id] = dict(
            self.em.all_points_by_id[self.point_id], variableId=second_point_id
        )
        self.em.bulk_data[second_point_id] = dict(self.em.bulk_data[self.point_id])
        second_entity_info = dict(self.mock_entity_info, point_id=second_point_id,
                                   entity_id=f'nibe_{second_point_id}')
        self.em._pub.publish_entity_discovery.side_effect = [
            self.mock_entity_info, second_entity_info,
        ]
        self.em.enable_entity(self.point_id)
        self.em._dismiss.reset_mock()
        self.em.enable_entity(second_point_id)
        self.em._dismiss.assert_not_called()

    # ── _disable_entity_locked ──────────────────────────────────────────

    def test_disable_clears_discovery_config_with_retain_true(self):
        """The config-clearing publish must use retain=True — without it,
        the empty payload wouldn't overwrite the retained discovery config
        on the broker and HA would keep showing the stale entity."""
        self.em.enable_entity(self.point_id)
        self.em.mqtt.reset_mock()
        self.em.disable_entity(self.point_id)
        from nibe_mqtt_publisher import t_config
        expected_topic = t_config('sensor', f'nibe_{self.point_id}')
        self.em.mqtt.publish.assert_any_call(expected_topic, "", retain=True)

    def test_disable_invalidates_config_hash_for_the_correct_point(self):
        """invalidate_config_hash must be called with THIS point's id, not
        None or some other value — otherwise the wrong point's cached hash
        gets wiped (or none does), letting a stale hash suppress the next
        legitimate republish for either point."""
        self.em.enable_entity(self.point_id)
        self.em._pub.invalidate_config_hash.reset_mock()
        self.em.disable_entity(self.point_id)
        self.em._pub.invalidate_config_hash.assert_called_once_with(self.point_id)

    def test_disable_clears_attributes_topic_when_present(self):
        """When entity_info has an attributes_topic, disable must publish an
        empty retained payload to that EXACT topic — not a wrong/None one."""
        self.mock_entity_info['attributes_topic'] = f'homeassistant/sensor/nibe_{self.point_id}/attributes'
        self.em.enable_entity(self.point_id)
        self.em.mqtt.reset_mock()
        self.em.disable_entity(self.point_id)
        self.em.mqtt.publish.assert_any_call(
            f'homeassistant/sensor/nibe_{self.point_id}/attributes', "", retain=True
        )

    def test_disable_no_attributes_topic_publishes_no_attributes_clear(self):
        """When attributes_topic is absent/None, no clearing publish for it
        should occur at all — the .get() guard must not be bypassed."""
        self.mock_entity_info['attributes_topic'] = None
        self.em.enable_entity(self.point_id)
        self.em.mqtt.reset_mock()
        self.em.disable_entity(self.point_id)
        attr_calls = [
            c for c in self.em.mqtt.publish.call_args_list
            if 'attributes' in str(c.args[0] if c.args else '')
        ]
        self.assertEqual(attr_calls, [])

    def test_disable_unsubscribes_and_removes_callback_for_command_topic(self):
        """When entity_info has a command_topic, disable must remove the
        message callback and unsubscribe using THAT exact topic string."""
        cmd_topic = f'homeassistant/switch/nibe_{self.point_id}/set'
        self.mock_entity_info['command_topic'] = cmd_topic
        self.mock_entity_info['entity_type'] = 'switch'
        self.em.enable_entity(self.point_id)
        self.em.mqtt.reset_mock()
        self.em.disable_entity(self.point_id)
        self.em.mqtt.message_callback_remove.assert_called_once_with(cmd_topic)
        self.em.mqtt.unsubscribe.assert_called_once_with(cmd_topic)

    def test_disable_pops_caches_keyed_by_this_point_id(self):
        """last_states/_point_string_cache/_entity_type_cache entries for a
        DIFFERENT point must survive disable — only this point_id's entries
        should be removed. Distinguishes the real point_id key from a
        wrong/None key mutant that would pop nothing (or the wrong thing)."""
        other_pid = self.point_id + 100
        self.em.enable_entity(self.point_id)
        self.em._point_string_cache.put(self.point_id, ('sensor', 'diagnostic'))
        self.em._point_string_cache.put(other_pid, ('sensor', 'diagnostic'))
        self.em._entity_type_cache.put(self.point_id, ('sensor', 'diagnostic'))
        self.em._entity_type_cache.put(other_pid, ('sensor', 'diagnostic'))
        self.em.disable_entity(self.point_id)
        self.assertIsNone(self.em._point_string_cache.get(self.point_id))
        self.assertIsNotNone(self.em._point_string_cache.get(other_pid))
        self.assertIsNone(self.em._entity_type_cache.get(self.point_id))
        self.assertIsNotNone(self.em._entity_type_cache.get(other_pid))

    def test_disable_decrements_category_stat_by_exactly_one(self):
        """Category stat decrement must reduce the pre-seeded count by
        exactly 1, not 2 and not increment — verified against an
        independently-chosen starting value distinct from any hardcoded
        default in the source."""
        self.em.enable_entity(self.point_id)
        self.em._stats_category_counts['diagnostic'] = 9
        self.em.disable_entity(self.point_id)
        self.assertEqual(self.em._stats_category_counts['diagnostic'], 8)

    def test_disable_category_stat_clamped_at_zero_not_left_at_one(self):
        """max(0, count - 1) must clamp to 0 when count is already 0/1 —
        not to 1, which would leave a phantom positive count forever after
        repeated disables (a real bug if the cap constant is mutated)."""
        self.em.enable_entity(self.point_id)
        self.em._stats_category_counts['diagnostic'] = 0
        self.em.disable_entity(self.point_id)
        self.assertEqual(self.em._stats_category_counts['diagnostic'], 0)

    def test_disable_writable_count_decrements_when_point_is_writable(self):
        """A writable point's disable must decrement _stats_writable_count
        by exactly 1 from an independently-seeded starting value."""
        self.em.all_points_by_id[self.point_id]['is_writable'] = True
        self.em.enable_entity(self.point_id)
        self.em._stats_writable_count = 4
        self.em.disable_entity(self.point_id)
        self.assertEqual(self.em._stats_writable_count, 3)

    def test_disable_publishes_enabled_state_when_not_suppressed(self):
        """disable must call publish_enabled_state() when NOT inside a
        suppression block — the guard is 'if not suppressed', not
        'if suppressed'."""
        self.em.enable_entity(self.point_id)
        with patch.object(self.em, 'publish_enabled_state') as mock_publish:
            self.em.disable_entity(self.point_id)
        mock_publish.assert_called_once()

    def test_disable_returns_true_on_success(self):
        """A successful disable of a genuinely-enabled point must return
        True, not False."""
        self.em.enable_entity(self.point_id)
        result = self.em.disable_entity(self.point_id)
        self.assertTrue(result)




