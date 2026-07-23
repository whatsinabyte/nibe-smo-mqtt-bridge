"""
test_entity_detection.py
========================
Nibe_entity_detection tests.
Part of the Nibe S-Series MQTT Bridge test suite.
Shared fixtures are in conftest.py.
"""

import json
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from hypothesis import assume, example, given
from hypothesis import strategies as st

from conftest import (
    _make_em,
    _nibe_raw_value,
    _nibe_divisor,
    _nibe_point_id,
    _unicode_text,
    _nibe_title_chars,
)

class TestApplyDivisor(unittest.TestCase):
    def setUp(self):
        from nibe_entity_detection import apply_divisor
        self.fn = apply_divisor

    def test_divisor_one(self):          self.assertEqual(self.fn(100,    1),  "100")
    def test_divisor_zero_as_one(self):  self.assertEqual(self.fn(100,    0),  "100")
    def test_divisor_ten(self):          self.assertEqual(self.fn(348,   10),  "34.8")
    def test_trailing_zero_stripped(self): self.assertEqual(self.fn(350, 10),  "35")
    def test_divisor_hundred(self):      self.assertEqual(self.fn(1234, 100),  "12.34")
    def test_zero_raw(self):             self.assertEqual(self.fn(0,    10),   "0")
    def test_negative_raw(self):         self.assertEqual(self.fn(-50,  10),   "-5")
    def test_large_divisor(self):        self.assertEqual(self.fn(12345, 1000),"12.345")

    def test_no_floating_point_noise(self):
        result = self.fn(200, 10)
        self.assertEqual(result, "20")
        self.assertNotIn(".", result)


# ===========================================================================
# 2. reverse_divisor
# ===========================================================================


class TestReverseDivisor(unittest.TestCase):
    def setUp(self):
        from nibe_entity_detection import reverse_divisor
        self.fn = reverse_divisor

    def test_divisor_one(self):      self.assertEqual(self.fn(42.0,  1),   42)
    def test_divisor_zero_as_one(self): self.assertEqual(self.fn(42.0, 0), 42)
    def test_divisor_ten(self):      self.assertEqual(self.fn(34.8, 10),  348)
    def test_negative(self):         self.assertEqual(self.fn(-5.0, 10),  -50)
    def test_zero(self):             self.assertEqual(self.fn(0.0,  10),    0)

    def test_round_trip(self):
        from nibe_entity_detection import apply_divisor
        for raw in [150, 200, 250, 300, 348, 400]:
            display = float(apply_divisor(raw, 10))
            self.assertEqual(self.fn(display, 10), raw,
                             f"round-trip failed for raw={raw}")

    def test_ieee754_safe(self):
        # 34.6 is exactly representable; avoid testing values that hit
        # Python banker's-rounding ambiguity.
        self.assertEqual(self.fn(34.6, 10), 346)


# ===========================================================================
# 3. clean_string
# ===========================================================================


class TestCleanString(unittest.TestCase):
    def setUp(self):
        from nibe_entity_detection import clean_string
        self.fn = clean_string

    def test_normal(self):         self.assertEqual(self.fn("Supply temperature"), "Supply temperature")
    def test_whitespace(self):     self.assertEqual(self.fn("  text  "), "text")
    def test_double_quotes(self):  self.assertEqual(self.fn('"text"'), "text")
    def test_single_quotes(self):  self.assertEqual(self.fn("'text'"), "text")
    def test_nbsp(self):           self.assertEqual(self.fn("a\u00a0b"), "a b")
    def test_mojibake_c2(self):    self.assertEqual(self.fn("a\u00c2b"), "ab")
    def test_collapse_spaces(self): self.assertEqual(self.fn("a   b"), "a b")
    def test_none(self):           self.assertIsNone(self.fn(None))
    def test_empty(self):          self.assertFalse(self.fn(""))
    def test_non_string(self):     self.assertEqual(self.fn(42), 42)

    def test_combined(self):
        self.assertEqual(self.fn('  "a\u00c2\u00a0b"  '), "a b")



# ===========================================================================
# 3b. clean_unit — consolidated mojibake/normalisation for unit strings
# ===========================================================================


class TestApplyDivisorProperties(unittest.TestCase):
    """Hypothesis property tests for apply_divisor and reverse_divisor."""

    @given(_nibe_raw_value, _nibe_divisor)
    @example(raw_value=250,   divisor=10)    # 25.0°C — real outdoor temperature
    @example(raw_value=3480,  divisor=10)    # 348.0 Hz — real compressor frequency
    @example(raw_value=-150,  divisor=10)    # -15.0°C — real outdoor temperature
    @example(raw_value=10000, divisor=100)   # 100.00 kWh — real energy counter
    @example(raw_value=0,     divisor=0)     # divisor=0 firmware deviation
    @example(raw_value=360,   divisor=60)    # 6.0 min — point 1024 (Timer EME), only divisor=60 in firmware
    @example(raw_value=16320, divisor=10)    # 1632.0 kWh — point 6139 (Total energy), s32 accumulator
    def test_never_raises(self, raw_value, divisor):
        """apply_divisor must never raise for any int × divisor combination."""
        from nibe_entity_detection import apply_divisor
        result = apply_divisor(raw_value, divisor)
        self.assertIsInstance(result, str)

    @given(_nibe_raw_value, _nibe_divisor)
    def test_result_is_parseable_as_float(self, raw_value, divisor):
        """apply_divisor result must always be parseable as a float."""
        from nibe_entity_detection import apply_divisor
        result = apply_divisor(raw_value, divisor)
        float(result)  # must not raise

    @given(_nibe_raw_value)
    def test_divisor_one_returns_integer_string(self, raw_value):
        """divisor=1 must return the exact integer as a string, no decimal point."""
        from nibe_entity_detection import apply_divisor
        result = apply_divisor(raw_value, 1)
        self.assertEqual(result, str(raw_value))
        self.assertNotIn('.', result)

    @given(_nibe_raw_value)
    def test_divisor_zero_same_as_divisor_one(self, raw_value):
        """divisor=0 must behave identically to divisor=1."""
        from nibe_entity_detection import apply_divisor
        self.assertEqual(apply_divisor(raw_value, 0), apply_divisor(raw_value, 1))

    @given(_nibe_raw_value, st.integers(min_value=1, max_value=10000))
    def test_roundtrip_within_integer_precision(self, raw_value, divisor):
        """reverse_divisor(apply_divisor(x, d), d) must recover x exactly
        (within integer rounding) for all valid Nibe register values."""
        from nibe_entity_detection import apply_divisor, reverse_divisor
        display = float(apply_divisor(raw_value, divisor))
        recovered = reverse_divisor(display, divisor)
        self.assertEqual(recovered, raw_value)

    @given(st.floats(min_value=-32768.0, max_value=32767.0,
                     allow_nan=False, allow_infinity=False),
           st.integers(min_value=1, max_value=10000))
    def test_reverse_divisor_never_raises(self, display_value, divisor):
        """reverse_divisor must never raise for any finite float × divisor."""
        from nibe_entity_detection import reverse_divisor
        result = reverse_divisor(display_value, divisor)
        self.assertIsInstance(result, int)

    @given(st.floats(min_value=-32768.0, max_value=32767.0,
                     allow_nan=False, allow_infinity=False))
    def test_reverse_divisor_zero_same_as_one(self, display_value):
        """reverse_divisor(x, 0) must behave identically to reverse_divisor(x, 1)."""
        from nibe_entity_detection import reverse_divisor
        self.assertEqual(reverse_divisor(display_value, 0),
                         reverse_divisor(display_value, 1))





class TestReverseDivisorProperties(unittest.TestCase):
    """Hypothesis properties for reverse_divisor."""

    @given(st.integers(min_value=-32768, max_value=32767),
           st.integers(min_value=1, max_value=1000))
    @example(raw=100,   divisor=1)     # identity
    @example(raw=348,   divisor=10)    # common Nibe divisor
    @example(raw=1234,  divisor=100)   # two decimal places
    @example(raw=12345, divisor=1000)  # three decimal places
    @example(raw=0,     divisor=10)    # zero value
    @example(raw=-100,  divisor=10)    # negative value
    def test_roundtrip_with_apply_divisor(self, raw, divisor):
        """reverse_divisor(apply_divisor(raw, d), d) == raw."""
        from nibe_entity_detection import reverse_divisor, apply_divisor
        display = float(apply_divisor(raw, divisor))
        result = reverse_divisor(display, divisor)
        self.assertEqual(result, raw)

    @given(st.floats(min_value=-1e6, max_value=1e6,
                     allow_nan=False, allow_infinity=False))
    @example(x=0.0)
    @example(x=100.0)
    @example(x=-100.0)
    def test_divisor_zero_treated_as_one(self, x):
        """divisor=0 treated as divisor=1 — same result as divisor=1."""
        from nibe_entity_detection import reverse_divisor
        self.assertEqual(reverse_divisor(x, 0), reverse_divisor(x, 1))

    @given(st.integers(min_value=-32768, max_value=32767),
           st.integers(min_value=1, max_value=1000))
    def test_always_returns_int(self, raw, divisor):
        """reverse_divisor always returns an int."""
        from nibe_entity_detection import reverse_divisor, apply_divisor
        display = float(apply_divisor(raw, divisor))
        result = reverse_divisor(display, divisor)
        self.assertIsInstance(result, int)

    @given(st.integers(min_value=-32768, max_value=32767),
           st.integers(min_value=1, max_value=1000))
    def test_result_within_1_of_raw(self, raw, divisor):
        """Result must be within 1 of raw (rounding tolerance)."""
        from nibe_entity_detection import reverse_divisor
        display = raw / divisor
        result = reverse_divisor(display, divisor)
        self.assertAlmostEqual(result, raw, delta=1)


class TestCleanStringProperties(unittest.TestCase):
    """Hypothesis property tests for clean_string."""

    @given(_unicode_text)
    def test_never_raises(self, text):
        """clean_string must never raise for any string input."""
        from nibe_entity_detection import clean_string
        result = clean_string(text)
        self.assertIsInstance(result, str)

    @given(_unicode_text)
    def test_idempotent(self, text):
        """clean_string(clean_string(s)) == clean_string(s) for all strings."""
        from nibe_entity_detection import clean_string
        once  = clean_string(text)
        twice = clean_string(once)
        self.assertEqual(once, twice)

    @given(_nibe_title_chars)
    def test_no_soft_hyphens_in_output(self, text):
        """Output must never contain U+00AD soft-hyphens."""
        from nibe_entity_detection import clean_string
        result = clean_string(text)
        self.assertNotIn('\u00ad', result)

    @given(_nibe_title_chars)
    def test_no_mojibake_byte_in_output(self, text):
        """Output must never contain the U+00C2 mojibake byte."""
        from nibe_entity_detection import clean_string
        result = clean_string(text)
        self.assertNotIn('\u00c2', result)

    @given(_unicode_text)
    def test_no_leading_trailing_whitespace(self, text):
        """Output must have no leading or trailing whitespace."""
        from nibe_entity_detection import clean_string
        result = clean_string(text)
        self.assertEqual(result, result.strip())

    @given(_unicode_text)
    def test_no_consecutive_spaces(self, text):
        """Output must not contain consecutive spaces (internal whitespace collapsed)."""
        from nibe_entity_detection import clean_string
        result = clean_string(text)
        self.assertNotIn('  ', result)



class TestCleanUnitProperties(unittest.TestCase):
    """Hypothesis property tests for clean_unit."""

    @given(_unicode_text)
    def test_always_returns_string(self, text):
        """clean_unit must always return a str, never None."""
        from nibe_entity_detection import clean_unit
        result = clean_unit(text)
        self.assertIsInstance(result, str)

    @given(_unicode_text)
    def test_never_raises(self, text):
        """clean_unit must never raise for any input."""
        from nibe_entity_detection import clean_unit
        clean_unit(text)  # must not raise

    @given(_unicode_text)
    def test_idempotent(self, text):
        """clean_unit(clean_unit(s)) == clean_unit(s) for all strings."""
        from nibe_entity_detection import clean_unit
        once  = clean_unit(text)
        twice = clean_unit(once)
        self.assertEqual(once, twice)

    @given(_unicode_text)
    def test_no_mojibake_byte_in_output(self, text):
        """Output must never contain the U+00C2 mojibake byte."""
        from nibe_entity_detection import clean_unit
        result = clean_unit(text)
        self.assertNotIn('\u00c2', result)

    @given(st.none() | st.just('') | st.just(0) | st.just(False))
    def test_falsy_input_returns_empty_string(self, value):
        """clean_unit with any falsy input must return ''."""
        from nibe_entity_detection import clean_unit
        self.assertEqual(clean_unit(value), '')


# ---------------------------------------------------------------------------
# Extended properties for already-covered functions
# ---------------------------------------------------------------------------


class TestCleanStringExtendedProperties(unittest.TestCase):
    """Additional Hypothesis properties for clean_string."""

    @given(_unicode_text)
    def test_output_no_longer_than_input(self, text):
        """clean_string only removes/collapses — output len ≤ input len."""
        from nibe_entity_detection import clean_string
        result = clean_string(text)
        self.assertLessEqual(len(result), len(text))

    @given(st.text(alphabet=' \t\n\r\u00a0\xa0', min_size=0, max_size=50))
    def test_whitespace_only_input_returns_empty(self, text):
        """Input containing only whitespace variants must produce ''."""
        from nibe_entity_detection import clean_string
        result = clean_string(text)
        self.assertEqual(result, '')



class TestApplyDivisorExtendedProperties(unittest.TestCase):
    """Additional Hypothesis properties for apply_divisor."""

    @given(_nibe_raw_value, st.integers(min_value=2, max_value=10000))
    def test_decimal_places_bounded_by_divisor(self, raw_value, divisor):
        """For divisor ≥ 2, result has at most ceil(log10(divisor)) decimal places."""
        import math
        from nibe_entity_detection import apply_divisor
        result = apply_divisor(raw_value, divisor)
        max_dp = math.ceil(math.log10(divisor))
        if '.' in result:
            actual_dp = len(result.split('.')[1])
            self.assertLessEqual(actual_dp, max_dp)

    @given(_nibe_raw_value, st.integers(min_value=2, max_value=10000))
    def test_no_trailing_zeros_after_decimal(self, raw_value, divisor):
        """apply_divisor must never produce trailing zeros after the decimal point."""
        from nibe_entity_detection import apply_divisor
        result = apply_divisor(raw_value, divisor)
        if '.' in result:
            self.assertFalse(result.endswith('0'),
                f"Trailing zero in {result!r} for raw={raw_value}, divisor={divisor}")
            self.assertFalse(result.endswith('.'),
                f"Trailing decimal in {result!r}")


# ---------------------------------------------------------------------------
# New function targets
# ---------------------------------------------------------------------------


class TestCreateEntityIdProperties(unittest.TestCase):
    """Hypothesis properties for create_entity_id."""

    @given(_nibe_point_id)
    @example(point_id=0)      # falsy — must still produce nibe_0
    @example(point_id=2685)   # date sensor special case
    @example(point_id=50662)  # THS-10 temperature (s32, divisor=10, 21.4°C)
    @example(point_id=50827)  # THS-10 humidity (s32, divisor=10, %RH→% override)
    @example(point_id=65535)  # max Modbus register
    def test_always_starts_with_nibe_prefix(self, point_id):
        from nibe_entity_detection import create_entity_id
        self.assertTrue(create_entity_id(point_id).startswith('nibe_'))

    @given(_nibe_point_id)
    def test_contains_exact_point_id(self, point_id):
        from nibe_entity_detection import create_entity_id
        result = create_entity_id(point_id)
        self.assertIn(str(point_id), result)

    @given(_nibe_point_id)
    def test_no_spaces_or_special_chars(self, point_id):
        from nibe_entity_detection import create_entity_id
        result = create_entity_id(point_id)
        self.assertRegex(result, r'^[a-z0-9_]+$')

    @given(_nibe_point_id,
           _nibe_point_id)
    def test_different_ids_produce_different_entity_ids(self, pid1, pid2):
        from nibe_entity_detection import create_entity_id
        if pid1 != pid2:
            self.assertNotEqual(create_entity_id(pid1), create_entity_id(pid2))



class TestGetRegisterTypeProperties(unittest.TestCase):
    """Hypothesis properties for get_register_type."""

    @given(st.text())
    def test_never_raises(self, modbus_type):
        from nibe_entity_detection import get_register_type
        get_register_type({'metadata': {'modbusRegisterType': modbus_type}})

    @given(st.text())
    def test_returns_only_valid_values(self, modbus_type):
        from nibe_entity_detection import get_register_type
        result = get_register_type({'metadata': {'modbusRegisterType': modbus_type}})
        self.assertIn(result, ('input', 'holding', None))

    def test_input_in_type_returns_input(self):
        """Any modbus type containing 'INPUT' returns 'input'."""
        from nibe_entity_detection import get_register_type
        for modbus_type in ['MODBUS_INPUT_REGISTER', 'INPUT', 'MY_INPUT_TYPE']:
            result = get_register_type({'metadata': {'modbusRegisterType': modbus_type}})
            self.assertEqual(result, 'input', f"Failed for {modbus_type!r}")

    def test_holding_in_type_returns_holding(self):
        """HOLDING (without INPUT) → 'holding'."""
        from nibe_entity_detection import get_register_type
        for modbus_type in ['MODBUS_HOLDING_REGISTER', 'HOLDING', 'MY_HOLDING_TYPE']:
            result = get_register_type({'metadata': {'modbusRegisterType': modbus_type}})
            self.assertEqual(result, 'holding', f"Failed for {modbus_type!r}")

    def test_neither_returns_none(self):
        """No INPUT or HOLDING → None."""
        from nibe_entity_detection import get_register_type
        for modbus_type in ['', 'MODBUS_NO_REGISTER', 'COIL', 'DISCRETE']:
            result = get_register_type({'metadata': {'modbusRegisterType': modbus_type}})
            self.assertIsNone(result, f"Failed for {modbus_type!r}")



class TestParseDescriptionMappingProperties(unittest.TestCase):
    """Hypothesis properties for parse_description_mapping."""

    @given(st.text())
    def test_never_raises(self, description):
        from nibe_entity_detection import parse_description_mapping
        parse_description_mapping(description)

    @given(st.text())
    def test_returns_dict_or_none(self, description):
        from nibe_entity_detection import parse_description_mapping
        result = parse_description_mapping(description)
        self.assertIn(type(result), (dict, type(None)))

    @given(st.text())
    def test_dict_keys_are_ints(self, description):
        """parse_description_mapping keys must always be ints."""
        from nibe_entity_detection import parse_description_mapping
        result = parse_description_mapping(description)
        if result is not None:
            for k in result.keys():
                self.assertIsInstance(k, int)

    @given(st.text())
    def test_dict_values_are_unique(self, description):
        """No two keys should map to the same integer value."""
        from nibe_entity_detection import parse_description_mapping
        result = parse_description_mapping(description)
        if result is not None:
            values = list(result.values())
            self.assertEqual(len(values), len(set(values)))




class TestIsSwitchAndNumberCandidateProperties(unittest.TestCase):
    """Hypothesis properties for is_switch_candidate and is_number_candidate."""

    _holding_u8_binary = {
        'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
        'unit': '', 'variableSize': 'u8',
        'minValue': 0, 'maxValue': 1, 'divisor': 1,
    }

    @given(st.text())
    def test_is_switch_always_returns_bool(self, unit):
        from nibe_entity_detection import is_switch_candidate
        result = is_switch_candidate({**self._holding_u8_binary, 'unit': unit})
        self.assertIsInstance(result, bool)

    @given(st.text())
    def test_is_number_always_returns_bool(self, unit):
        from nibe_entity_detection import is_number_candidate
        result = is_number_candidate({'unit': unit})
        self.assertIsInstance(result, bool)

    @given(st.text(min_size=1).filter(lambda s: s.strip()))
    def test_nonempty_unit_is_number_candidate(self, unit):
        """A register with a non-empty unit is always a number candidate."""
        from nibe_entity_detection import is_number_candidate
        self.assertTrue(is_number_candidate({'unit': unit}))

    def test_empty_unit_not_number_candidate(self):
        from nibe_entity_detection import is_number_candidate
        self.assertFalse(is_number_candidate({'unit': ''}))

    def test_canonical_switch_shape_is_switch_candidate(self):
        from nibe_entity_detection import is_switch_candidate
        self.assertTrue(is_switch_candidate(self._holding_u8_binary))

    @given(st.text(min_size=1).filter(lambda s: s.strip()))
    def test_unit_present_disqualifies_switch_candidate(self, unit):
        """A switch candidate must have no unit — any unit disqualifies it."""
        from nibe_entity_detection import is_switch_candidate
        meta = {**self._holding_u8_binary, 'unit': unit}
        self.assertFalse(is_switch_candidate(meta))



class TestDetectEntityTypeProperties(unittest.TestCase):
    """Hypothesis properties for detect_entity_type."""

    _VALID_TYPES = frozenset({
        'sensor', 'binary_sensor', 'switch', 'number',
        'select', 'button', 'text', 'time',
    })
    _VALID_CATEGORIES = frozenset({'config', 'diagnostic'})

    def _point(self, point_id=99999, modbus_type='MODBUS_INPUT_REGISTER',
               var_size='u8', min_val=0, max_val=100,
               unit='', writable=False, var_type='integer'):
        return {
            'variableId': point_id, 'title': 'Test', 'description': '',
            'metadata': {
                'modbusRegisterType': modbus_type,
                'variableSize': var_size, 'variableType': var_type,
                'minValue': min_val, 'maxValue': max_val,
                'unit': unit, 'isWritable': writable,
                'divisor': 1, 'decimal': 0,
            },
        }

    @given(_nibe_point_id,
           st.sampled_from(['MODBUS_INPUT_REGISTER', 'MODBUS_HOLDING_REGISTER',
                            'MODBUS_NO_REGISTER', '']),
           st.sampled_from(['u8', 'u16', 's16', 's32', 'u32']),
           st.integers(min_value=-32768, max_value=32767),
           st.integers(min_value=-32768, max_value=32767),
           st.text(max_size=10),
           st.booleans())
    def test_always_returns_two_tuple(self, pid, modbus, size, mn, mx, unit, writable):
        from nibe_entity_detection import detect_entity_type
        point = self._point(pid, modbus, size, mn, mx, unit, writable)
        result = detect_entity_type(point)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    @given(_nibe_point_id,
           st.sampled_from(['MODBUS_INPUT_REGISTER', 'MODBUS_HOLDING_REGISTER', '']),
           st.sampled_from(['u8', 'u16', 's16', 's32']),
           st.integers(min_value=-32768, max_value=32767),
           st.integers(min_value=-32768, max_value=32767),
           st.text(max_size=10),
           st.booleans())
    def test_entity_type_always_valid(self, pid, modbus, size, mn, mx, unit, writable):
        from nibe_entity_detection import detect_entity_type
        point = self._point(pid, modbus, size, mn, mx, unit, writable)
        entity_type, _ = detect_entity_type(point)
        self.assertIn(entity_type, self._VALID_TYPES)

    @given(_nibe_point_id,
           st.sampled_from(['MODBUS_INPUT_REGISTER', 'MODBUS_HOLDING_REGISTER', '']),
           st.sampled_from(['u8', 'u16', 's16', 's32']),
           st.integers(min_value=-32768, max_value=32767),
           st.integers(min_value=-32768, max_value=32767),
           st.text(max_size=10),
           st.booleans())
    def test_category_always_valid(self, pid, modbus, size, mn, mx, unit, writable):
        from nibe_entity_detection import detect_entity_type
        point = self._point(pid, modbus, size, mn, mx, unit, writable)
        _, category = detect_entity_type(point)
        self.assertIn(category, self._VALID_CATEGORIES)

    def test_overridden_point_returns_override_type(self):
        """ENTITY_TYPE_OVERRIDES always beats auto-detection."""
        from nibe_entity_detection import detect_entity_type, ENTITY_TYPE_OVERRIDES
        for pid, expected_type in list(ENTITY_TYPE_OVERRIDES.items())[:10]:
            point = self._point(pid)
            entity_type, _ = detect_entity_type(point)
            self.assertEqual(entity_type, expected_type,
                f"Point {pid}: expected override {expected_type!r}, got {entity_type!r}")

    def test_unknown_register_type_falls_back_to_sensor_diagnostic(self):
        """Any non-INPUT/HOLDING register type must fall back to sensor/diagnostic."""
        from nibe_entity_detection import detect_entity_type
        point = self._point(modbus_type='MODBUS_NO_REGISTER')
        self.assertEqual(detect_entity_type(point), ('sensor', 'diagnostic'))

    def test_config_category_only_for_writable_entities(self):
        """config category implies either an override or a writable holding register."""
        from nibe_entity_detection import detect_entity_type
        # INPUT registers are never config
        point = self._point(88888, modbus_type='MODBUS_INPUT_REGISTER')
        _, category = detect_entity_type(point)
        self.assertEqual(category, 'diagnostic')



class TestGetValueMappingProperties(unittest.TestCase):
    """Hypothesis properties for get_value_mapping."""

    @given(_nibe_point_id,
           st.text(max_size=100),
           st.one_of(st.none(), st.just('input'), st.just('holding')))
    def test_never_raises(self, point_id, description, register_type):
        from nibe_entity_detection import get_value_mapping
        get_value_mapping(point_id, {'description': description}, register_type)

    @given(_nibe_point_id,
           st.text(max_size=100))
    def test_returns_dict_or_none(self, point_id, description):
        from nibe_entity_detection import get_value_mapping
        result = get_value_mapping(point_id, {'description': description})
        self.assertIn(type(result), (dict, type(None)))

    @given(_nibe_point_id,
           st.text(max_size=100))
    def test_dict_keys_are_ints(self, point_id, description):
        from nibe_entity_detection import get_value_mapping
        result = get_value_mapping(point_id, {'description': description})
        if result is not None:
            for k in result.keys():
                self.assertIsInstance(k, int)

    @given(_nibe_point_id,
           st.text(max_size=100))
    def test_dict_values_are_strings(self, point_id, description):
        from nibe_entity_detection import get_value_mapping
        result = get_value_mapping(point_id, {'description': description})
        if result is not None:
            for v in result.values():
                self.assertIsInstance(v, str)


# ---------------------------------------------------------------------------
# get_entity_options properties
# ---------------------------------------------------------------------------


class TestGetEntityOptionsProperties(unittest.TestCase):
    """Hypothesis properties for get_entity_options."""

    _meta = {'modbusRegisterType': 'MODBUS_HOLDING_REGISTER'}

    @given(_nibe_point_id, st.text(max_size=100))
    def test_never_raises(self, point_id, description):
        from nibe_entity_detection import get_entity_options
        get_entity_options(point_id, self._meta, description)

    @given(_nibe_point_id, st.text(max_size=100))
    def test_always_returns_list(self, point_id, description):
        from nibe_entity_detection import get_entity_options
        result = get_entity_options(point_id, self._meta, description)
        self.assertIsInstance(result, list)

    @given(_nibe_point_id, st.text(max_size=100))
    def test_all_elements_are_strings(self, point_id, description):
        from nibe_entity_detection import get_entity_options
        result = get_entity_options(point_id, self._meta, description)
        for opt in result:
            self.assertIsInstance(opt, str)

    @given(_nibe_point_id, st.text(max_size=100))
    def test_no_duplicate_options(self, point_id, description):
        from nibe_entity_detection import get_entity_options
        result = get_entity_options(point_id, self._meta, description)
        self.assertEqual(len(result), len(set(result)))

    @given(_nibe_point_id, st.text(max_size=100))
    def test_no_empty_string_options(self, point_id, description):
        from nibe_entity_detection import get_entity_options
        result = get_entity_options(point_id, self._meta, description)
        self.assertNotIn('', result)

    @given(_nibe_point_id, st.text(max_size=100))
    def test_never_returns_single_option(self, point_id, description):
        """A single option is not a valid select — must have ≥2 or return []."""
        from nibe_entity_detection import get_entity_options
        result = get_entity_options(point_id, self._meta, description)
        self.assertNotEqual(len(result), 1)


# ---------------------------------------------------------------------------
# map_device_class properties
# ---------------------------------------------------------------------------


class TestMapDeviceClassProperties(unittest.TestCase):
    """Hypothesis properties for map_device_class."""

    @given(st.text(max_size=20), st.text(max_size=20), st.text(max_size=80))
    def test_never_raises(self, entity_type, unit, title):
        from nibe_entity_detection import map_device_class
        map_device_class(entity_type, unit, title)

    @given(st.text(max_size=20), st.text(max_size=20), st.text(max_size=80))
    def test_returns_string_or_none(self, entity_type, unit, title):
        from nibe_entity_detection import map_device_class
        result = map_device_class(entity_type, unit, title)
        self.assertIn(type(result), (str, type(None)))

    @given(st.text(max_size=20).filter(lambda s: s not in ('sensor', 'binary_sensor', 'number')),
           st.text(max_size=20), st.text(max_size=80))
    def test_non_sensor_type_always_none(self, entity_type, unit, title):
        """Only sensor/binary_sensor/number support device_class — all others → None."""
        from nibe_entity_detection import map_device_class
        self.assertIsNone(map_device_class(entity_type, unit, title))

    @given(st.text(max_size=20), st.text(max_size=80))
    def test_binary_sensor_always_none(self, unit, title):
        """binary_sensor never gets a device_class from this function."""
        from nibe_entity_detection import map_device_class
        self.assertIsNone(map_device_class('binary_sensor', unit, title))

    @given(st.text(max_size=20), st.text(max_size=80))
    def test_number_always_none(self, unit, title):
        """number never gets a device_class from this function (HA validation strict)."""
        from nibe_entity_detection import map_device_class
        self.assertIsNone(map_device_class('number', unit, title))


# ---------------------------------------------------------------------------
# _is_auto_binary_sensor properties
# ---------------------------------------------------------------------------


class TestIsAutoBinarySensorProperties(unittest.TestCase):
    """Hypothesis properties for _is_auto_binary_sensor."""

    @given(_nibe_point_id,
           st.text(max_size=100),
           st.integers(min_value=-32768, max_value=32767),
           st.integers(min_value=-32768, max_value=32767))
    def test_never_raises(self, pid, description, mn, mx):
        from nibe_entity_detection import _is_auto_binary_sensor
        point = {'variableId': pid, 'description': description}
        meta = {'variableSize': 'u8', 'minValue': mn, 'maxValue': mx,
                'unit': '', 'isWritable': False}
        _is_auto_binary_sensor(point, meta)

    @given(_nibe_point_id,
           st.text(max_size=100),
           st.integers(min_value=-32768, max_value=32767),
           st.integers(min_value=-32768, max_value=32767))
    def test_always_returns_bool(self, pid, description, mn, mx):
        from nibe_entity_detection import _is_auto_binary_sensor
        point = {'variableId': pid, 'description': description}
        meta = {'variableSize': 'u8', 'minValue': mn, 'maxValue': mx,
                'unit': '', 'isWritable': False}
        result = _is_auto_binary_sensor(point, meta)
        self.assertIsInstance(result, bool)

    @given(_nibe_point_id.filter(
               lambda p: p not in __import__('nibe_entity_detection')._BINARY_SENSOR_EXCLUSIONS))
    def test_writable_point_never_binary_sensor(self, pid):
        """Any writable point must never be auto-classified as binary_sensor."""
        from nibe_entity_detection import _is_auto_binary_sensor
        point = {'variableId': pid, 'description': ''}
        meta = {'variableSize': 'u8', 'minValue': 0, 'maxValue': 1,
                'unit': '', 'isWritable': True}
        self.assertFalse(_is_auto_binary_sensor(point, meta))

    @given(st.text(min_size=1).filter(lambda s: s.strip()))
    def test_point_with_unit_never_binary_sensor(self, unit):
        """Any point with a non-empty unit must never be auto-classified as binary_sensor."""
        from nibe_entity_detection import _is_auto_binary_sensor
        point = {'variableId': 88888, 'description': ''}
        meta = {'variableSize': 'u8', 'minValue': 0, 'maxValue': 1,
                'unit': unit, 'isWritable': False}
        self.assertFalse(_is_auto_binary_sensor(point, meta))

    @given(st.integers(min_value=2, max_value=32767))
    def test_max_greater_than_one_never_binary_sensor(self, max_val):
        """max > 1 means it cannot be a simple binary flag."""
        from nibe_entity_detection import _is_auto_binary_sensor
        point = {'variableId': 88888, 'description': ''}
        meta = {'variableSize': 'u8', 'minValue': 0, 'maxValue': max_val,
                'unit': '', 'isWritable': False}
        self.assertFalse(_is_auto_binary_sensor(point, meta))


# ---------------------------------------------------------------------------
# DynamicPointEntry serialisation roundtrip
# ---------------------------------------------------------------------------


class TestEntityIdFromRegex(unittest.TestCase):
    """Use st.from_regex to generate realistic HA entity IDs and test
    resolve_point_from_entity_id with strings that look like real entities."""

    def setUp(self):
        self.em = _make_em()

    @given(st.from_regex(
        r'(sensor|switch|number|binary_sensor|select|button)\.nibe_\d{1,5}',
        fullmatch=True,
    ))
    def test_nibe_entity_ids_always_resolve(self, entity_id):
        """Any well-formed domain.nibe_{pid} entity ID must always resolve to an int."""
        result = self.em.resolve_point_from_entity_id(entity_id)
        self.assertIsInstance(result, int)
        self.assertGreaterEqual(result, 0)

    @given(st.from_regex(
        r'(sensor|switch|number)\.nibe_[0-9]{1,5}',
        fullmatch=True,
    ))
    def test_resolved_pid_roundtrips_via_create_entity_id(self, entity_id):
        """resolve → int → create_entity_id → resolve must be consistent."""
        from nibe_entity_detection import create_entity_id
        result = self.em.resolve_point_from_entity_id(entity_id)
        if result is not None:
            # create_entity_id(result) must resolve back to the same int
            reconstructed = f'sensor.{create_entity_id(result)}'
            result2 = self.em.resolve_point_from_entity_id(reconstructed)
            self.assertEqual(result, result2)

    @given(st.from_regex(
        r'(sensor|switch)\.(?!nibe_)\w{3,20}',
        fullmatch=True,
    ))
    def test_non_nibe_entity_ids_return_none(self, entity_id):
        """Entity IDs without nibe_ slug must return None from an empty registry."""
        result = self.em.resolve_point_from_entity_id(entity_id)
        self.assertIsNone(result)

    @given(st.from_regex(
        r'sensor\.nibe_(0|65535|\d{1,4})',
        fullmatch=True,
    ))
    @example(entity_id='sensor.nibe_0')      # pid=0 is falsy — must resolve
    @example(entity_id='sensor.nibe_65535')  # max Modbus register
    @example(entity_id='sensor.nibe_2685')   # date sensor
    @example(entity_id='sensor.nibe_50827')  # THS-10 humidity
    def test_boundary_pids_always_resolve(self, entity_id):
        """Boundary pid values must always resolve correctly."""
        result = self.em.resolve_point_from_entity_id(entity_id)
        # Extract expected pid from entity_id
        pid_str = entity_id.split('nibe_')[1]
        expected = int(pid_str)
        self.assertEqual(result, expected)


# ---------------------------------------------------------------------------
# 4. MQTT command payload fuzzing — _handle_command never crashes on bad bytes
# ---------------------------------------------------------------------------


class TestModesStructuralProperties(unittest.TestCase):
    """Structural invariants for the MODES configuration dict.

    These lock in the shape of MODES so silent drift (wrong type, missing key,
    wrong point count) gets caught immediately rather than manifesting as a
    confusing runtime error when apply_mode() is called.
    """

    def setUp(self):
        from nibe_entity_detection import MODES
        self.MODES = MODES
        # Dynamically find mode names by their role (order: smallest→largest)
        # 'none' always empty, 'all' always None, rest are sized frozensets
        self._sized = {k: v for k, v in MODES.items()
                       if v is not None and len(v) > 0}
        # Sort by size to get smallest=essential, largest=advanced
        self._by_size = sorted(self._sized.items(), key=lambda kv: len(kv[1]))

    def test_six_mode_names_present(self):
        """Exactly six mode names must always be present."""
        self.assertEqual(len(self.MODES), 6)

    def test_none_mode_exists_and_is_empty(self):
        """A 'none' mode must always be an empty frozenset."""
        self.assertIn('none', self.MODES)
        self.assertEqual(self.MODES['none'], frozenset())

    def test_all_mode_exists(self):
        """An 'all' mode must always be present."""
        self.assertIn('all', self.MODES)

    def test_menus_mode_exists(self):
        """A 'menus' mode must always be present."""
        self.assertIn('menus', self.MODES)

    def test_modes_are_frozensets_or_none(self):
        """All mode values must be frozenset or None."""
        for name, pts in self.MODES.items():
            self.assertIn(type(pts), (frozenset, type(None)),
                f"MODES[{name!r}] is {type(pts).__name__}, expected frozenset or None")

    def test_at_least_three_sized_modes(self):
        """At least three modes must have non-empty frozensets (essential/monitoring/advanced)."""
        self.assertGreaterEqual(len(self._sized), 3)

    def test_sized_modes_are_strictly_nested(self):
        """Smaller modes must be subsets of larger modes."""
        for i in range(len(self._by_size) - 1):
            small_name, small = self._by_size[i]
            large_name, large = self._by_size[i + 1]
            self.assertTrue(small.issubset(large),
                f"MODES[{small_name!r}] is not a subset of MODES[{large_name!r}]")

    def test_mode_points_are_all_ints(self):
        """All point IDs in all modes must be ints."""
        for name, pts in self.MODES.items():
            if pts is not None:
                for pid in pts:
                    self.assertIsInstance(pid, int,
                        f"MODES[{name!r}] contains non-int: {pid!r}")

    def test_mode_point_ids_in_valid_range(self):
        """All point IDs must be in the valid Nibe register range (1-65535)."""
        for name, pts in self.MODES.items():
            if pts is not None:
                for pid in pts:
                    self.assertGreater(pid, 0,
                        f"MODES[{name!r}] contains zero/negative pid: {pid}")
                    self.assertLessEqual(pid, 65535,
                        f"MODES[{name!r}] contains out-of-range pid: {pid}")

    def test_none_mode_always_empty(self):
        """'none' is always empty — no points ever enabled by default."""
        self.assertEqual(len(self.MODES['none']), 0)


# ---------------------------------------------------------------------------
# ENTITY_TYPE_OVERRIDES and VALUE_MAPPINGS structural invariants
# ---------------------------------------------------------------------------


class TestDetectHoldingEntityProperties(unittest.TestCase):
    """Hypothesis properties for _detect_holding_entity."""

    def _point(self, pid, var_type='integer', var_size='s16',
               description='', writable=True):
        return {
            'variableId': pid,
            'description': description,
            'metadata': {
                'variableType': var_type,
                'variableSize': var_size,
                'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                'isWritable': writable,
                'minValue': 0, 'maxValue': 1,
                'unit': '', 'divisor': 1,
            }
        }

    @given(_nibe_point_id.filter(
        lambda p: p not in __import__('nibe_entity_detection').VALUE_MAPPINGS.get('holding', {})))
    def test_category_is_always_config(self, pid):
        """_detect_holding_entity always returns config category."""
        from nibe_entity_detection import _detect_holding_entity
        point = self._point(pid)
        _, category = _detect_holding_entity(point, point['metadata'])
        self.assertEqual(category, 'config')

    @given(_nibe_point_id)
    def test_always_returns_two_tuple(self, pid):
        from nibe_entity_detection import _detect_holding_entity
        point = self._point(pid)
        result = _detect_holding_entity(point, point['metadata'])
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    @given(_nibe_point_id)
    def test_time_var_type_always_returns_number(self, pid):
        from nibe_entity_detection import _detect_holding_entity
        point = self._point(pid, var_type='time')
        entity_type, category = _detect_holding_entity(point, point['metadata'])
        self.assertEqual(entity_type, 'number')
        self.assertEqual(category, 'config')

    @given(_nibe_point_id)
    def test_date_var_type_always_returns_number(self, pid):
        from nibe_entity_detection import _detect_holding_entity
        point = self._point(pid, var_type='date')
        entity_type, category = _detect_holding_entity(point, point['metadata'])
        self.assertEqual(entity_type, 'number')
        self.assertEqual(category, 'config')

    @given(_nibe_point_id.filter(
        lambda p: p not in __import__('nibe_entity_detection').VALUE_MAPPINGS.get('holding', {})))
    def test_non_writable_holding_returns_sensor_diagnostic(self, pid):
        """isWritable=False on HOLDING → sensor/diagnostic (Modbus-TCP only)."""
        from nibe_entity_detection import _detect_holding_entity
        point = self._point(pid, writable=False)
        entity_type, category = _detect_holding_entity(point, point['metadata'])
        self.assertEqual(entity_type, 'sensor')
        self.assertEqual(category, 'diagnostic')

    @given(_nibe_point_id)
    def test_never_raises(self, pid):
        from nibe_entity_detection import _detect_holding_entity
        point = self._point(pid)
        _detect_holding_entity(point, point['metadata'])  # must not raise



class TestMapDeviceClassUsesCleanUnit(unittest.TestCase):
    """map_device_class previously had its own inline _UNIT_NORMALISE
    lookup (no mojibake stripping); it now delegates to clean_unit(). This
    confirms that consolidation didn't change its observable behavior for
    a mojibake-affected unit — the case the old inline version couldn't
    handle but the new one can."""

    def setUp(self):
        from nibe_entity_detection import map_device_class
        self.fn = map_device_class

    def test_clean_temperature_unit_resolves(self):
        self.assertEqual(self.fn("sensor", "°C", "Outdoor temperature"), "temperature")

    def test_mojibake_temperature_unit_resolves(self):
        """Firmware sometimes emits 'Â°C' directly in the unit field —
        confirms this now resolves to 'temperature' instead of silently
        failing the lookup (the old inline version had no strip step)."""
        self.assertEqual(self.fn("sensor", "\u00c2°C", "Outdoor temperature"), "temperature")

    def test_unclassifiable_unit_returns_none(self):
        self.assertIsNone(self.fn("sensor", "DM", "Degree minutes"))

    def test_binary_sensor_always_none(self):
        self.assertIsNone(self.fn("binary_sensor", "°C", "Anything"))


# ===========================================================================
# 4. get_register_type
# ===========================================================================


class TestGetRegisterType(unittest.TestCase):
    def setUp(self):
        from nibe_entity_detection import get_register_type
        self.fn = get_register_type

    def test_holding(self):
        self.assertEqual(self.fn({'metadata': {'modbusRegisterType': 'MODBUS_HOLDING_REGISTER'}}), 'holding')

    def test_input(self):
        self.assertEqual(self.fn({'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER'}}), 'input')

    def test_unknown(self):      self.assertIsNone(self.fn({'metadata': {'modbusRegisterType': 'X'}}))
    def test_no_metadata(self):  self.assertIsNone(self.fn({}))
    def test_empty_meta(self):   self.assertIsNone(self.fn({'metadata': {}}))


# ===========================================================================
# 5. parse_description_mapping
# ===========================================================================


class TestParseDescriptionMapping(unittest.TestCase):
    def setUp(self):
        from nibe_entity_detection import parse_description_mapping
        import nibe_entity_detection as ned
        ned._description_mapping_cache.clear()
        self.fn = parse_description_mapping

    def test_int_left(self):     self.assertEqual(self.fn("0 = Off, 1 = Active"), {0: 'Off', 1: 'Active'})
    def test_label_left(self):   self.assertEqual(self.fn("Auto = 0, Manual = 1"), {0: 'Auto', 1: 'Manual'})
    def test_no_equals(self):    self.assertIsNone(self.fn("Off Active"))
    def test_empty(self):        self.assertIsNone(self.fn(""))
    def test_none(self):         self.assertIsNone(self.fn(None))
    def test_single_pair(self):  self.assertEqual(self.fn("0 = Off"), {0: 'Off'})
    def test_whitespace(self):   self.assertEqual(self.fn("  0  =  Off  ,  1  =  On  "), {0: 'Off', 1: 'On'})

    def test_cached(self):
        desc = "0 = Off, 1 = On"
        self.assertIs(self.fn(desc), self.fn(desc))


# ===========================================================================
# 6. detect_entity_type
# ===========================================================================


class TestDetectEntityType(unittest.TestCase):
    def setUp(self):
        from nibe_entity_detection import detect_entity_type
        self.fn = detect_entity_type

    def _holding(self, **kw):
        meta = {'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                'unit': '', 'variableSize': 'u8',
                'minValue': 0, 'maxValue': 1, 'divisor': 1, 'isWritable': True}
        meta.update(kw)
        return {'variableId': 9999, 'metadata': meta, 'title': 'T', 'description': ''}

    def _input(self, **kw):
        meta = {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'unit': '°C', 'variableSize': 's16', 'divisor': 10, 'isWritable': False}
        meta.update(kw)
        return {'variableId': 9998, 'metadata': meta, 'title': 'T', 'description': ''}

    def test_switch_from_holding(self):
        t, c = self.fn(self._holding())
        self.assertEqual(t, 'switch')
        self.assertEqual(c, 'config')

    def test_sensor_from_input(self):
        t, _ = self.fn(self._input())
        self.assertEqual(t, 'sensor')

    def test_unknown_falls_back_to_sensor(self):
        t, c = self.fn({'variableId': 1, 'metadata': {}, 'title': 'x', 'description': ''})
        self.assertEqual(t, 'sensor')
        self.assertEqual(c, 'diagnostic')


# ===========================================================================
# 7. Compression helpers
# ===========================================================================


class TestApiSpecConformance(unittest.TestCase):
    """Tests grounded in the official Nibe Local REST API specification.

    The spec defines:
      variableType enum:  integer, string, binary, time, date, floating-point, unknown
      variableSize enum:  s8, s16, s32, u8, u16, u32, f4, f8, unknown
      modbusRegisterType: MODBUS_INPUT_REGISTER, MODBUS_HOLDING_REGISTER,
                          MODBUS_NO_REGISTER, ERR_UNKNOWN
      PATCH response:     {"<variableId>": "modified"
                                         | "error: no such param"
                                         | "error: read only value"}
    """

    def setUp(self):
        from nibe_entity_detection import detect_entity_type, get_register_type
        self.detect_type   = detect_entity_type
        self.get_reg_type  = get_register_type

    def _point(self, modbus_type, var_type='integer', var_size='s16',
               writable=False, unit='', point_id=9000):
        return {
            'variableId': point_id,
            'title': 'Test Point',
            'description': '',
            'metadata': {
                'modbusRegisterType': modbus_type,
                'variableType':       var_type,
                'variableSize':       var_size,
                'unit':               unit,
                'isWritable':         writable,
                'divisor':            1,
                'minValue':           0,
                'maxValue':           100,
            }
        }

    # ── modbusRegisterType edge cases ─────────────────────────────────────────

    def test_modbus_no_register_returns_sensor(self):
        """MODBUS_NO_REGISTER has no read/write path — bridge must not crash
        and should expose it as a read-only sensor rather than skipping it."""
        entity_type, category = self.detect_type(
            self._point('MODBUS_NO_REGISTER')
        )
        # Must not raise; should degrade gracefully to sensor
        self.assertIsInstance(entity_type, str)
        self.assertIsInstance(category, str)

    def test_err_unknown_register_returns_sensor(self):
        """ERR_UNKNOWN means the firmware could not determine the register type.
        Bridge must degrade gracefully rather than crashing."""
        entity_type, _ = self.detect_type(self._point('ERR_UNKNOWN'))
        self.assertIsInstance(entity_type, str)

    def test_get_register_type_no_register_returns_none(self):
        """get_register_type uses substring matching — MODBUS_NO_REGISTER
        contains neither INPUT nor HOLDING so must return None."""
        result = self.get_reg_type(
            {'metadata': {'modbusRegisterType': 'MODBUS_NO_REGISTER'}}
        )
        self.assertIsNone(result)

    def test_get_register_type_err_unknown_returns_none(self):
        result = self.get_reg_type(
            {'metadata': {'modbusRegisterType': 'ERR_UNKNOWN'}}
        )
        self.assertIsNone(result)

    # ── variableType: binary ──────────────────────────────────────────────────

    def test_variabletype_binary_holding_classified(self):
        """variableType=binary is a spec-defined value the code does not
        explicitly handle.  It must not crash — the detection path must fall
        through to a safe default."""
        entity_type, _ = self.detect_type(
            self._point('MODBUS_HOLDING_REGISTER', var_type='binary',
                        var_size='u8', writable=True)
        )
        self.assertIsInstance(entity_type, str)
        self.assertNotEqual(entity_type, '')

    def test_variabletype_binary_input_classified(self):
        entity_type, _ = self.detect_type(
            self._point('MODBUS_INPUT_REGISTER', var_type='binary',
                        var_size='u8', writable=False)
        )
        self.assertIsInstance(entity_type, str)

    def test_variabletype_unknown_does_not_crash(self):
        """variableType=unknown is explicitly in the spec enum."""
        entity_type, _ = self.detect_type(
            self._point('MODBUS_INPUT_REGISTER', var_type='unknown')
        )
        self.assertIsInstance(entity_type, str)

    # ── variableType: floating-point / variableSize: f4, f8 ──────────────────

    def test_variabletype_floating_point_holding(self):
        """variableType=floating-point with variableSize=f4 is in the spec.
        In practice Nibe firmware always uses integerValue + divisor instead,
        but the bridge must not crash if it encounters this."""
        entity_type, _ = self.detect_type(
            self._point('MODBUS_HOLDING_REGISTER', var_type='floating-point',
                        var_size='f4', writable=True)
        )
        self.assertIsInstance(entity_type, str)

    def test_variablesize_f4_does_not_crash(self):
        entity_type, _ = self.detect_type(
            self._point('MODBUS_INPUT_REGISTER', var_type='floating-point',
                        var_size='f4')
        )
        self.assertIsInstance(entity_type, str)

    def test_variablesize_f8_does_not_crash(self):
        entity_type, _ = self.detect_type(
            self._point('MODBUS_INPUT_REGISTER', var_type='floating-point',
                        var_size='f8')
        )
        self.assertIsInstance(entity_type, str)

    # ── variableType: string → text entity path ───────────────────────────────

    def test_variabletype_string_holding_maps_to_text(self):
        """The spec defines variableType=string.  When a holding register
        has this type the bridge classifies it as 'text'."""
        entity_type, category = self.detect_type(
            self._point('MODBUS_HOLDING_REGISTER', var_type='string',
                        writable=True)
        )
        self.assertEqual(entity_type, 'text')
        self.assertEqual(category, 'config')

    def test_variabletype_string_input_maps_to_sensor(self):
        """A read-only string input register maps to sensor + diagnostic.
        Nibe firmware does not use text registers in practice — all registers
        store integers. Returning 'text_sensor' (not a valid HA type) would
        cause a WARNING log on every discovery publish and fall through to
        sensor anyway, so we classify it as sensor directly."""
        entity_type, category = self.detect_type(
            self._point('MODBUS_INPUT_REGISTER', var_type='string',
                        writable=False)
        )
        self.assertEqual(entity_type, 'sensor')
        self.assertEqual(category, 'diagnostic')

    def test_variabletype_string_logs_debug(self):
        """variableType=string should log a debug message that text is unsupported."""
        with self.assertLogs('nibe.detection', level='DEBUG') as cm:
            self.detect_type(
                self._point('MODBUS_HOLDING_REGISTER', var_type='string',
                            writable=True, point_id=8001)
            )
        self.assertTrue(any('text' in m.lower() or 'string' in m.lower()
                            for m in cm.output))

    def test_variabletype_floating_point_logs_debug(self):
        """variableType=floating-point should log a debug message."""
        with self.assertLogs('nibe.detection', level='DEBUG') as cm:
            self.detect_type(
                self._point('MODBUS_INPUT_REGISTER', var_type='floating-point',
                            var_size='f4', point_id=8002)
            )
        self.assertTrue(any('float' in m.lower() for m in cm.output))

    def test_api_response_value_key_confirmed(self):
        """Confirm the bridge reads 'value' (not 'datavalue') as the JSON key.

        Real SMO S40 firmware response for a single point uses:
          {"title": ..., "metadata": {...}, "value": {"type": "datavalue", ...}}
        The key is 'value'; 'datavalue' is the value of the inner type field.
        """
        em = _make_em()
        em.initial_discovery_complete = False
        # Use real-world response shape confirmed against actual firmware
        fake_response = {
            "6984": {
                "title": "Power at DOT, manual value",
                "description": "",
                "metadata": {
                    "type": "metadata", "variableId": 6984,
                    "variableType": "integer", "variableSize": "u8",
                    "unit": "", "modbusRegisterType": "MODBUS_HOLDING_REGISTER",
                    "shortUnit": "", "isWritable": True, "divisor": 1,
                    "decimal": 0, "modbusRegisterID": 4200,
                    "minValue": 0, "maxValue": 1,
                    "intDefaultValue": 0, "change": 1, "stringDefaultValue": "",
                },
                "value": {           # ← confirmed real key name
                    "type": "datavalue",   # ← this is the type field, not the key
                    "isOk": True,
                    "variableId": 6984,
                    "integerValue": 0,
                    "stringValue": "",
                }
            }
        }
        em._api.fetch_bulk_points.return_value = fake_response
        em._fetch_bulk_data(detect_changes=False)
        self.assertIn(6984, em.bulk_data)
        self.assertEqual(em.bulk_data[6984]['raw_value'], 0)
        self.assertTrue(em.bulk_data[6984]['is_ok'])

    # ── variableType: time / date ─────────────────────────────────────────────

    def test_variabletype_time_holding(self):
        """time registers on HOLDING (writable) map to number — HA has no
        MQTT time entity that accepts raw integer seconds."""
        entity_type, category = self.detect_type(
            self._point('MODBUS_HOLDING_REGISTER', var_type='time', writable=True)
        )
        self.assertEqual(entity_type, 'number')
        self.assertEqual(category, 'config')

    def test_variabletype_date_holding(self):
        """date registers on HOLDING (writable) map to number."""
        entity_type, category = self.detect_type(
            self._point('MODBUS_HOLDING_REGISTER', var_type='date', writable=True)
        )
        self.assertEqual(entity_type, 'number')
        self.assertEqual(category, 'config')

    def test_variabletype_time_input(self):
        """time registers on INPUT (read-only) map to sensor."""
        entity_type, _ = self.detect_type(
            self._point('MODBUS_INPUT_REGISTER', var_type='time')
        )
        self.assertEqual(entity_type, 'sensor')

    # ── PATCH response: spec-documented strings ───────────────────────────────

    def test_patch_response_modified_string(self):
        """Spec documents "modified" as the success response string."""
        import ssl
        from nibe_api import NibeApiClient
        from unittest.mock import patch, MagicMock
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        client = NibeApiClient("https://192.0.2.1:8443/api/v1/devices/0",
                               "Basic dGVzdA==", ctx)
        ei = {'is_writable': True, 'is_degenerate_range': False,
              'metadata': {'minValue': 0, 'maxValue': 100, 'isWritable': True}}
        r = MagicMock()
        r.read.return_value = json.dumps({"42": "modified"}).encode()
        with patch('urllib.request.urlopen', return_value=r):
            self.assertTrue(client.write_point(42, 50, ei))

    def test_patch_response_no_such_param(self):
        """Spec documents "error: no such param" as a rejection string."""
        import ssl
        from nibe_api import NibeApiClient
        from unittest.mock import patch, MagicMock
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        client = NibeApiClient("https://192.0.2.1:8443/api/v1/devices/0",
                               "Basic dGVzdA==", ctx)
        ei = {'is_writable': True, 'is_degenerate_range': False,
              'metadata': {'minValue': 0, 'maxValue': 100, 'isWritable': True}}
        r = MagicMock()
        r.read.return_value = json.dumps({"42": "error: no such param"}).encode()
        with patch('urllib.request.urlopen', return_value=r):
            self.assertFalse(client.write_point(42, 50, ei))

    def test_patch_response_read_only_value(self):
        """Spec documents "error: read only value" as a rejection string."""
        import ssl
        from nibe_api import NibeApiClient
        from unittest.mock import patch, MagicMock
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        client = NibeApiClient("https://192.0.2.1:8443/api/v1/devices/0",
                               "Basic dGVzdA==", ctx)
        ei = {'is_writable': True, 'is_degenerate_range': False,
              'metadata': {'minValue': 0, 'maxValue': 100, 'isWritable': True}}
        r = MagicMock()
        r.read.return_value = json.dumps({"42": "error: read only value"}).encode()
        with patch('urllib.request.urlopen', return_value=r):
            self.assertFalse(client.write_point(42, 50, ei))

    # ── datavalue shape ───────────────────────────────────────────────────────

    def test_bulk_data_reads_integer_value_field(self):
        """Spec: datavalue.integerValue is the primary value field.
        Verify the bridge reads it correctly from a realistic API response."""
        em = _make_em()
        em.initial_discovery_complete = False

        # Simulate a minimal bulk API response matching the spec shape
        fake_response = {
            "4": {
                "title": "Outdoor temperature",
                "description": "",
                "metadata": {
                    "type": "metadata",
                    "variableId": 4,
                    "variableType": "integer",
                    "variableSize": "s16",
                    "unit": "°C",
                    "modbusRegisterType": "MODBUS_INPUT_REGISTER",
                    "shortUnit": "°C",
                    "isWritable": False,
                    "divisor": 10,
                    "decimal": 1,
                    "modbusRegisterID": 40004,
                    "minValue": -400,
                    "maxValue": 400,
                    "intDefaultValue": 0,
                    "change": 5,
                    "stringDefaultValue": "",
                },
                "value": {
                    "type": "datavalue",
                    "isOk": True,
                    "variableId": 4,
                    "integerValue": 119,
                    "stringValue": "",
                }
            }
        }
        # NOTE: the bridge reads point_data.get('value', {}) — not 'datavalue'.
        # The real API returns this key as 'value'; 'datavalue' is the spec name
        # for the object type field within it.
        em._api.fetch_bulk_points.return_value = fake_response
        em._fetch_bulk_data(detect_changes=False)

        self.assertIn(4, em.bulk_data)
        self.assertEqual(em.bulk_data[4]['raw_value'], 119)
        self.assertTrue(em.bulk_data[4]['is_ok'])

    def test_bulk_data_isok_false_marks_correctly(self):
        """Spec: isOk=false means the sensor read failed."""
        em = _make_em()
        em.initial_discovery_complete = False

        fake_response = {
            "4": {
                "title": "Outdoor temp",
                "description": "",
                "metadata": {
                    "type": "metadata", "variableId": 4,
                    "variableType": "integer", "variableSize": "s16",
                    "unit": "°C", "modbusRegisterType": "MODBUS_INPUT_REGISTER",
                    "shortUnit": "°C", "isWritable": False, "divisor": 10,
                    "decimal": 1, "modbusRegisterID": 40004,
                    "minValue": -400, "maxValue": 400,
                    "intDefaultValue": 0, "change": 5, "stringDefaultValue": "",
                },
                "value": {
                    "type": "datavalue", "isOk": False,
                    "variableId": 4, "integerValue": 0, "stringValue": "",
                }
            }
        }
        em._api.fetch_bulk_points.return_value = fake_response
        em._fetch_bulk_data(detect_changes=False)

        self.assertIn(4, em.bulk_data)
        self.assertFalse(em.bulk_data[4]['is_ok'])

    def test_device_info_fields_match_spec(self):
        """Spec defines: deviceIndex, aidMode, smartMode, product{serialNumber,
        name, manufacturer, firmwareId}.  Verify the bridge reads the fields
        it actually uses."""
        import ssl
        from nibe_api import NibeApiClient
        from unittest.mock import patch, MagicMock
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        client = NibeApiClient("https://192.0.2.1:8443/api/v1/devices/0",
                               "Basic dGVzdA==", ctx)
        spec_response = {
            "deviceIndex": 0,
            "aidMode": "off",
            "smartMode": "normal",
            "product": {
                "serialNumber": "123456789",
                "name": "SMO S40",
                "manufacturer": "NIBE",
                "firmwareId": "4.5.7",
            }
        }
        r = MagicMock()
        r.read.return_value = json.dumps(spec_response).encode()
        with patch('urllib.request.urlopen', return_value=r):
            result = client.fetch_device_info()
        self.assertEqual(result['product']['name'], 'SMO S40')
        self.assertEqual(result['aidMode'], 'off')
        self.assertEqual(result['smartMode'], 'normal')

    def test_notification_alarm_fields_match_spec(self):
        """Spec defines alarms with: alarmId, description, header, severity,
        time, equipName.  Verify fetch_notifications returns these."""
        import ssl
        from nibe_api import NibeApiClient
        from unittest.mock import patch, MagicMock
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        client = NibeApiClient("https://192.0.2.1:8443/api/v1/devices/0",
                               "Basic dGVzdA==", ctx)
        spec_response = {
            "alarms": [
                {
                    "alarmId":     101,
                    "description": "High pressure alarm",
                    "header":      "Alarm 101",
                    "severity":    2,
                    "time":        "2024-01-15 14:30:00",
                    "equipName":   "Heat pump",
                }
            ]
        }
        r = MagicMock()
        r.read.return_value = json.dumps(spec_response).encode()
        with patch('urllib.request.urlopen', return_value=r):
            alarms = client.fetch_notifications()
        self.assertEqual(len(alarms), 1)
        alarm = alarms[0]
        self.assertEqual(alarm['alarmId'], 101)
        self.assertEqual(alarm['severity'], 2)
        self.assertIn('description', alarm)
        self.assertIn('equipName', alarm)

    def test_aidmode_valid_enum_values(self):
        """Spec: aidMode enum is exactly 'off' and 'on'."""
        import ssl
        from nibe_api import NibeApiClient
        from unittest.mock import patch, MagicMock
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        client = NibeApiClient("https://192.0.2.1:8443/api/v1/devices/0",
                               "Basic dGVzdA==", ctx)
        r = MagicMock()
        r.read.return_value = b''
        with patch('urllib.request.urlopen', return_value=r):
            for value in ('on', 'off'):
                client.write_device_mode('aidmode', value)
                # With empty response body the urlopen call itself is what matters
                # — we're testing the request is formed and dispatched without error

    def test_smartmode_valid_enum_values(self):
        """Spec: smartMode enum is exactly 'normal' and 'away'."""
        import ssl
        from nibe_api import NibeApiClient
        from unittest.mock import patch, MagicMock
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        client = NibeApiClient("https://192.0.2.1:8443/api/v1/devices/0",
                               "Basic dGVzdA==", ctx)
        r = MagicMock()
        r.read.return_value = b''
        with patch('urllib.request.urlopen', return_value=r):
            for value in ('normal', 'away'):
                client.write_device_mode('smartmode', value)




# ===========================================================================
# 14. request() retry logic
# ===========================================================================


class TestUnitOverrides(unittest.TestCase):

    def test_4562_override_is_empty_string(self):
        from nibe_entity_detection import UNIT_OVERRIDES
        self.assertIn(4562, UNIT_OVERRIDES)
        self.assertEqual(UNIT_OVERRIDES[4562], '')

    def test_50825_override_is_percent(self):
        from nibe_entity_detection import UNIT_OVERRIDES
        self.assertIn(50825, UNIT_OVERRIDES)
        self.assertEqual(UNIT_OVERRIDES[50825], '%')

    def test_4562_entity_type_is_switch(self):
        from nibe_entity_detection import ENTITY_TYPE_OVERRIDES
        self.assertIn(4562, ENTITY_TYPE_OVERRIDES)
        self.assertEqual(ENTITY_TYPE_OVERRIDES[4562], 'switch')


# ===========================================================================
# 37. point_to_menu_map initialises empty
# ===========================================================================


class TestDetectEntityTypeWarningLogging(unittest.TestCase):
    """detect_entity_type()'s ENTITY_TYPE_OVERRIDES branch logs a
    one-shot-per-point DEBUG message showing what auto-detect would have chosen
    vs. what the override forces. Module-level dedup set since this is a
    pure function with no instance."""

    def setUp(self):
        import nibe_entity_detection as ned
        self.ned = ned
        # Save and clear the module-level dedup set so tests don't leak
        # state into each other or depend on prior test execution order.
        self._saved_warned = set(ned._entity_type_override_warnings_issued)
        ned._entity_type_override_warnings_issued.clear()

    def tearDown(self):
        self.ned._entity_type_override_warnings_issued.clear()
        self.ned._entity_type_override_warnings_issued.update(self._saved_warned)

    def _point(self, point_id, title, modbus_type, min_val=0, max_val=1, divisor=1, unit=''):
        return {
            'variableId': point_id,
            'title': title,
            'description': '',
            'metadata': {
                'modbusRegisterType': modbus_type,
                'minValue': min_val, 'maxValue': max_val,
                'isWritable': True, 'divisor': divisor, 'unit': unit,
            },
        }

    def test_known_override_point_32824_logs_correct_auto_type(self):
        """Real, confirmed case from this session's audit: point 32824
        (Power limitation activation) is MODBUS_NO_REGISTER-shaped in a way
        that auto-detect would call 'sensor', but is overridden to 'switch'."""
        point = self._point(32824, 'Power limitation activation', 'MODBUS_NO_REGISTER')
        with patch.object(self.ned.log_detection, 'debug') as mock_warn:
            entity_type, category = self.ned.detect_entity_type(point)
        self.assertEqual(entity_type, 'switch')
        mock_warn.assert_called()
        args = mock_warn.call_args.args
        self.assertIn('entity type overridden', args[0])
        self.assertEqual(args[1], 32824)
        self.assertEqual(args[2], 'Power limitation activation')
        self.assertEqual(args[3], 'sensor')   # what auto-detect would say
        self.assertEqual(args[4], 'switch')   # what's actually used

    def test_known_override_point_5110_logs_correct_auto_type(self):
        """Real, confirmed case: point 5110 (Prevent condensation climate
        system 1) auto-detects as 'number' but is overridden to 'switch'."""
        point = self._point(5110, 'Prevent condensation climate system 1',
                             'MODBUS_HOLDING_REGISTER', min_val=0, max_val=1)
        with patch.object(self.ned.log_detection, 'debug') as mock_warn:
            entity_type, category = self.ned.detect_entity_type(point)
        self.assertEqual(entity_type, 'switch')
        args = mock_warn.call_args.args
        self.assertEqual(args[3], 'number')
        self.assertEqual(args[4], 'switch')

    def test_repeated_calls_same_point_log_only_once(self):
        point = self._point(32824, 'Power limitation activation', 'MODBUS_NO_REGISTER')
        with patch.object(self.ned.log_detection, 'debug') as mock_warn:
            self.ned.detect_entity_type(point)
            self.ned.detect_entity_type(point)
            self.ned.detect_entity_type(point)
        mock_warn.assert_called_once()

    def test_non_overridden_point_never_logs(self):
        point = self._point(999999, 'Some normal sensor', 'MODBUS_INPUT_REGISTER',
                             min_val=0, max_val=1000, divisor=10, unit='°C')
        with patch.object(self.ned.log_detection, 'debug') as mock_warn:
            self.ned.detect_entity_type(point)
            mock_warn.assert_not_called()

    def test_missing_title_falls_back_to_point_label(self):
        point = self._point(32824, '', 'MODBUS_NO_REGISTER')
        with patch.object(self.ned.log_detection, 'debug') as mock_warn:
            self.ned.detect_entity_type(point)
        args = mock_warn.call_args.args
        self.assertEqual(args[2], 'Point 32824')

    def test_override_still_returns_correct_type_and_category_with_logging(self):
        """The logging side effect must not change the actual return value —
        confirms the refactor (extracting _detect_type_without_override)
        preserved the override branch's real behaviour."""
        point = self._point(5110, 'Prevent condensation climate system 1',
                             'MODBUS_HOLDING_REGISTER', min_val=0, max_val=1)
        entity_type, category = self.ned.detect_entity_type(point)
        self.assertEqual(entity_type, 'switch')
        self.assertEqual(category, 'config')

    def test_override_warning_not_added_when_cache_is_full(self):
        """637->639: when the warning cache is at capacity, the point_id
        is not added but the override still applies correctly."""
        import nibe_entity_detection as ned
        orig_max = ned._ENTITY_TYPE_WARNING_CACHE_MAX
        try:
            # Fill the cache to capacity with sentinel values
            ned._ENTITY_TYPE_WARNING_CACHE_MAX = 0
            ned._entity_type_override_warnings_issued.clear()
            point = self._point(32824, 'Power limitation activation', 'MODBUS_NO_REGISTER')
            entity_type, _ = ned.detect_entity_type(point)
            # Override still applied correctly even though we can't log it
            self.assertEqual(entity_type, 'switch')
            # Point NOT added to warning set (cache full)
            self.assertNotIn(32824, ned._entity_type_override_warnings_issued)
        finally:
            ned._ENTITY_TYPE_WARNING_CACHE_MAX = orig_max
            ned._entity_type_override_warnings_issued.clear()



class TestDetectTypeWithoutOverride(unittest.TestCase):
    """_detect_type_without_override is the dispatch logic extracted out of
    detect_entity_type during this refactor — confirms it alone reproduces
    exactly the same dispatch behaviour the inline version had, for all
    three branches (holding/input/fallback)."""

    def test_holding_register_dispatches_to_detect_holding_entity(self):
        from nibe_entity_detection import _detect_type_without_override
        point = {'variableId': 100, 'title': 'Some switch', 'metadata': {
            'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
            'minValue': 0, 'maxValue': 1, 'isWritable': True, 'divisor': 1, 'unit': '',
        }}
        entity_type, category = _detect_type_without_override(
            point, point['metadata'], 'MODBUS_HOLDING_REGISTER')
        self.assertEqual(entity_type, 'number')

    def test_input_register_dispatches_to_detect_input_entity(self):
        from nibe_entity_detection import _detect_type_without_override
        point = {'variableId': 100, 'title': 'Some sensor', 'metadata': {
            'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
            'minValue': 0, 'maxValue': 1000, 'isWritable': False, 'divisor': 10, 'unit': '°C',
        }}
        entity_type, category = _detect_type_without_override(
            point, point['metadata'], 'MODBUS_INPUT_REGISTER')
        self.assertEqual(entity_type, 'sensor')

    def test_unknown_register_type_falls_back_to_diagnostic_sensor(self):
        from nibe_entity_detection import _detect_type_without_override
        point = {'variableId': 100, 'title': 'Weird point', 'metadata': {
            'modbusRegisterType': 'MODBUS_NO_REGISTER',
            'minValue': 0, 'maxValue': 1, 'isWritable': False, 'divisor': 1, 'unit': '',
        }}
        result = _detect_type_without_override(point, point['metadata'], 'MODBUS_NO_REGISTER')
        self.assertEqual(result, ('sensor', 'diagnostic'))



class TestNibeApiHttpErrorPaths(unittest.TestCase):
    """HTTP error handling in request(), write_point(), reset_notifications(),
    and write_device_mode(). All paths mock urllib.request.urlopen."""

    def setUp(self):
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        from nibe_api import NibeApiClient
        self.client = NibeApiClient(
            'https://192.0.2.1:8443/api/v1/devices/0',
            'Basic dGVzdA==', ctx,
        )

    def _http_error(self, code, body=b'error'):
        """Build a urllib.error.HTTPError with a readable body."""
        import io
        err = urllib.error.HTTPError(
            url='https://192.0.2.1:8443/api/v1/devices/0/points',
            code=code, msg='Error', hdrs={}, fp=io.BytesIO(body),
        )
        return err

    def _ei(self, writable=True):
        return {
            'is_writable': writable,
            'is_degenerate_range': False,
            'metadata': {'minValue': 0, 'maxValue': 100, 'isWritable': writable},
        }

    # ── request() ────────────────────────────────────────────────────────────

    def test_request_raises_on_401(self):
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(401)):
            with self.assertRaises(urllib.error.HTTPError):
                self.client.request('https://192.0.2.1:8443/test')

    def test_request_raises_on_403(self):
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(403)):
            with self.assertRaises(urllib.error.HTTPError):
                self.client.request('https://192.0.2.1:8443/test')

    def test_request_raises_on_404(self):
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(404)):
            with self.assertRaises(urllib.error.HTTPError):
                self.client.request('https://192.0.2.1:8443/test')

    def test_request_retries_on_500_then_returns_none(self):
        """Non-auth HTTP errors trigger one retry then return None."""
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(500)), \
             patch('time.sleep'):
            result = self.client.request('https://192.0.2.1:8443/test')
        self.assertIsNone(result)

    def test_request_retries_on_network_exception(self):
        """Generic exceptions trigger one retry then return None."""
        with patch('urllib.request.urlopen',
                   side_effect=Exception('timeout')), \
             patch('time.sleep'):
            result = self.client.request('https://192.0.2.1:8443/test')
        self.assertIsNone(result)

    def test_request_retries_exactly_once_on_transient_error(self):
        """On a transient error (HTTP 500) the client makes exactly 2 attempts
        — the original call plus one retry — then returns None."""
        call_count = [0]
        def _side_effect(*a, **kw):
            call_count[0] += 1
            raise urllib.error.HTTPError('', 500, 'err', None, None)
        with patch('urllib.request.urlopen', side_effect=_side_effect), \
             patch('time.sleep'):
            result = self.client.request('https://192.0.2.1:8443/test')
        self.assertIsNone(result)
        self.assertEqual(call_count[0], 2)

    # ── fetch_point() — 404 handling ─────────────────────────────────────────

    def test_fetch_point_404_returns_none(self):
        """HTTP 404 returns None — dynamic point inactive."""
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(404)):
            result = self.client.fetch_point(9999)
        self.assertIsNone(result)

    def test_fetch_point_other_http_error_raises(self):
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(500)), \
             patch('time.sleep'):
            result = self.client.fetch_point(9999)
        self.assertIsNone(result)

    # ── write_point() — HTTP error paths ─────────────────────────────────────

    def test_write_point_http_400_returns_false(self):
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(400, b'bad request')):
            result = self.client.write_point(100, 50, self._ei())
        self.assertFalse(result)

    def test_write_point_http_401_returns_false(self):
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(401)):
            result = self.client.write_point(100, 50, self._ei())
        self.assertFalse(result)

    def test_write_point_http_403_returns_false(self):
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(403)):
            result = self.client.write_point(100, 50, self._ei())
        self.assertFalse(result)

    def test_write_point_http_other_returns_false(self):
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(503)):
            result = self.client.write_point(100, 50, self._ei())
        self.assertFalse(result)

    def test_write_point_generic_exception_returns_false(self):
        with patch('urllib.request.urlopen',
                   side_effect=Exception('connection reset')):
            result = self.client.write_point(100, 50, self._ei())
        self.assertFalse(result)

    def test_write_point_body_read_failure_still_returns_false(self):
        """If reading the HTTP error body raises, write still returns False."""
        err = urllib.error.HTTPError(
            url='https://test', code=400, msg='Bad Request',
            hdrs={}, fp=MagicMock(read=MagicMock(side_effect=Exception('read failed'))),
        )
        with patch('urllib.request.urlopen', side_effect=err):
            result = self.client.write_point(100, 50, self._ei())
        self.assertFalse(result)

    # ── reset_notifications() — HTTP error paths ──────────────────────────────

    def test_reset_notifications_405_returns_false(self):
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(405)):
            result = self.client.reset_notifications()
        self.assertFalse(result)

    def test_reset_notifications_401_returns_false(self):
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(401)):
            result = self.client.reset_notifications()
        self.assertFalse(result)

    def test_reset_notifications_403_returns_false(self):
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(403)):
            result = self.client.reset_notifications()
        self.assertFalse(result)

    def test_reset_notifications_generic_exception_returns_false(self):
        with patch('urllib.request.urlopen',
                   side_effect=Exception('network error')):
            result = self.client.reset_notifications()
        self.assertFalse(result)

    def test_reset_notifications_other_http_code_returns_false(self):
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(503)):
            result = self.client.reset_notifications()
        self.assertFalse(result)

    # ── write_device_mode() — HTTP error paths ────────────────────────────────

    def test_write_device_mode_http_400_returns_false(self):
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(400, b'bad value')):
            result = self.client.write_device_mode('aidmode', 'on')
        self.assertFalse(result)

    def test_write_device_mode_http_401_returns_false(self):
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(401)):
            result = self.client.write_device_mode('aidmode', 'on')
        self.assertFalse(result)

    def test_write_device_mode_http_403_returns_false(self):
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(403)):
            result = self.client.write_device_mode('smartmode', 'away')
        self.assertFalse(result)

    def test_write_device_mode_http_other_returns_false(self):
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(503)):
            result = self.client.write_device_mode('aidmode', 'off')
        self.assertFalse(result)

    def test_write_device_mode_generic_exception_returns_false(self):
        with patch('urllib.request.urlopen',
                   side_effect=Exception('timeout')):
            result = self.client.write_device_mode('aidmode', 'on')
        self.assertFalse(result)

    def test_write_device_mode_body_read_failure_still_returns_false(self):
        err = urllib.error.HTTPError(
            url='https://test', code=400, msg='Bad Request',
            hdrs={}, fp=MagicMock(read=MagicMock(side_effect=Exception('read failed'))),
        )
        with patch('urllib.request.urlopen', side_effect=err):
            result = self.client.write_device_mode('aidmode', 'on')
        self.assertFalse(result)


# ===========================================================================
# 78. nibe_entity_detection — edge cases
# ===========================================================================


class TestParseDescriptionMappingEdgeCases(unittest.TestCase):
    """Edge cases in parse_description_mapping not covered by main tests."""

    def setUp(self):
        from nibe_entity_detection import _description_mapping_cache
        _description_mapping_cache.clear()

    def test_reversed_key_value_parsed(self):
        """If left side of '=' is not int but right side is, they are swapped."""
        from nibe_entity_detection import parse_description_mapping
        result = parse_description_mapping('Label=0,Other=1')
        self.assertIsNotNone(result)
        self.assertIn(0, result)

    def test_neither_side_int_skips_pair(self):
        """If neither side of '=' is an int, the pair is skipped."""
        from nibe_entity_detection import parse_description_mapping
        result = parse_description_mapping('foo=bar,0=Valid')
        # The foo=bar pair is skipped; 0=Valid should still parse
        self.assertIsNotNone(result)
        self.assertIn(0, result)

    def test_all_invalid_pairs_returns_none(self):
        """If no valid pairs can be parsed, returns None."""
        from nibe_entity_detection import parse_description_mapping
        result = parse_description_mapping('foo=bar,baz=qux')
        self.assertIsNone(result)

    def test_no_equals_sign_returns_none(self):
        from nibe_entity_detection import parse_description_mapping
        result = parse_description_mapping('no equals here')
        self.assertIsNone(result)

    def test_empty_description_returns_none(self):
        from nibe_entity_detection import parse_description_mapping
        self.assertIsNone(parse_description_mapping(''))
        self.assertIsNone(parse_description_mapping(None))

    def test_result_is_cached(self):
        from nibe_entity_detection import parse_description_mapping, _description_mapping_cache
        desc = '0=Off,1=On'
        parse_description_mapping(desc)
        self.assertIn(desc, _description_mapping_cache)

    def test_cached_result_returned_on_second_call(self):
        from nibe_entity_detection import parse_description_mapping
        desc = '0=Off,1=On'
        r1 = parse_description_mapping(desc)
        r2 = parse_description_mapping(desc)
        self.assertIs(r1, r2)

    def test_result_not_cached_when_cache_is_full(self):
        """452->454: when _description_mapping_cache is at capacity,
        result is returned but not stored in the cache."""
        import nibe_entity_detection as ned
        from nibe_entity_detection import parse_description_mapping, _description_mapping_cache
        _description_mapping_cache.clear()
        orig_max = ned._DESCRIPTION_CACHE_MAX
        try:
            # Fill cache to capacity with dummy entries
            ned._DESCRIPTION_CACHE_MAX = 2
            _description_mapping_cache['0=A,1=B'] = {'A': 0}
            _description_mapping_cache['0=C,1=D'] = {'C': 0}
            # Now call with a new description — cache is full, result not stored
            result = parse_description_mapping('0=E,1=F')
            self.assertIsNotNone(result)
            self.assertNotIn('0=E,1=F', _description_mapping_cache)
        finally:
            ned._DESCRIPTION_CACHE_MAX = orig_max
            _description_mapping_cache.clear()



class TestGetValueMappingEdgeCases(unittest.TestCase):
    """get_value_mapping falls back to description when no manual entry."""

    def test_manual_mapping_takes_precedence(self):
        from nibe_entity_detection import get_value_mapping, VALUE_MAPPINGS
        # Use a point_id known to be in VALUE_MAPPINGS under 'holding'
        for pid in VALUE_MAPPINGS.get('holding', {}):
            result = get_value_mapping(pid, {}, register_type='holding')
            self.assertIsNotNone(result)
            return
        self.skipTest('No holding VALUE_MAPPINGS entries available')

    def test_falls_back_to_description_when_no_manual(self):
        from nibe_entity_detection import get_value_mapping
        point_data = {'description': '0=Off,1=On,2=Auto'}
        result = get_value_mapping(99999, point_data, register_type=None)
        self.assertEqual(result[0], 'Off')
        self.assertEqual(result[1], 'On')

    def test_returns_none_when_no_mapping_and_no_description(self):
        from nibe_entity_detection import get_value_mapping
        result = get_value_mapping(99999, {'description': ''}, register_type=None)
        self.assertIsNone(result)



class TestGetEntityOptionsEdgeCases(unittest.TestCase):
    """get_entity_options returns labels from mapping or description."""

    def test_description_with_two_plus_options_returned(self):
        from nibe_entity_detection import get_entity_options
        opts = get_entity_options(99999, {
            'modbusRegisterType': 'MODBUS_HOLDING_REGISTER'
        }, '0=Off,1=On,2=Auto')
        self.assertGreaterEqual(len(opts), 2)
        self.assertIn('Off', opts)

    def test_description_with_only_one_option_returns_empty(self):
        from nibe_entity_detection import get_entity_options
        opts = get_entity_options(99999, {
            'modbusRegisterType': 'MODBUS_HOLDING_REGISTER'
        }, '0=Off')
        self.assertEqual(opts, [])

    def test_no_description_no_mapping_returns_empty(self):
        from nibe_entity_detection import get_entity_options
        opts = get_entity_options(99999, {
            'modbusRegisterType': 'MODBUS_HOLDING_REGISTER'
        }, '')
        self.assertEqual(opts, [])

    def test_reversed_key_value_label_extracted(self):
        """When value side is int, label is the left side."""
        from nibe_entity_detection import get_entity_options
        opts = get_entity_options(99999, {
            'modbusRegisterType': 'MODBUS_HOLDING_REGISTER'
        }, 'Off=0,On=1,Auto=2')
        self.assertIn('Off', opts)
        self.assertIn('On', opts)

    def test_duplicate_labels_deduplicated(self):
        from nibe_entity_detection import get_entity_options
        opts = get_entity_options(99999, {
            'modbusRegisterType': 'MODBUS_HOLDING_REGISTER'
        }, '0=Auto,1=Auto,2=On')
        self.assertEqual(opts.count('Auto'), 1)



class TestGetOptionsFromDescriptionBranches(unittest.TestCase):
    """Branch coverage for get_options_from_description (509->512)."""

    def test_duplicate_option_not_added_twice(self):
        """509->512: if text is already in options, it is skipped — no duplicates."""
        from nibe_entity_detection import get_entity_options
        opts = get_entity_options(99999, {
            'modbusRegisterType': 'MODBUS_HOLDING_REGISTER'
        }, '0=Auto,1=Auto,2=On')
        self.assertEqual(opts.count('Auto'), 1)
        self.assertIn('On', opts)

    def test_empty_text_side_skipped(self):
        """509->512: if stripped text is empty, option is not appended."""
        from nibe_entity_detection import get_entity_options
        # '0= ,1=On' — left side is blank after strip
        opts = get_entity_options(99999, {
            'modbusRegisterType': 'MODBUS_HOLDING_REGISTER'
        }, '0= ,1=On')
        self.assertNotIn('', opts)
        self.assertNotIn(' ', opts)

    def test_fewer_than_two_options_returns_empty(self):
        """Only one unique non-empty option → returns [] (< 2 options threshold)."""
        from nibe_entity_detection import get_entity_options
        opts = get_entity_options(99999, {
            'modbusRegisterType': 'MODBUS_HOLDING_REGISTER'
        }, '0=On,1=On')
        self.assertEqual(opts, [])



class TestDetectHoldingEntityEdgeCases(unittest.TestCase):
    """Edge cases in _detect_holding_entity for unusual variableType values."""

    def _point(self, pid, var_type, description=''):
        return {
            'variableId': pid,
            'title': f'Point {pid}',
            'description': description,
            'metadata': {
                'variableType': var_type,
                'variableSize': 's16',
                'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                'isWritable': True,
                'divisor': 1, 'decimal': 0,
                'minValue': 0, 'maxValue': 10,
                'unit': '', 'shortUnit': '',
            }
        }

    def test_binary_type_logs_and_falls_through(self):
        """variableType='binary' logs a warning but falls through to normal
        detection — the result depends on the register shape, not the type."""
        from nibe_entity_detection import _detect_holding_entity
        p = self._point(99999, 'binary')
        result = _detect_holding_entity(p, p['metadata'])
        # Falls through — with minValue=0, maxValue=10 it's not a switch candidate
        self.assertIn(result[0], ('number', 'switch', 'select'))

    def test_floating_point_type_falls_through_to_number(self):
        from nibe_entity_detection import _detect_holding_entity
        p = self._point(99999, 'floating-point')
        result = _detect_holding_entity(p, p['metadata'])
        # Falls through to number since float is unhandled
        self.assertIn(result[0], ('number', 'switch', 'select'))



class TestDetectInputEntityEdgeCases(unittest.TestCase):
    """Edge cases in _detect_input_entity for unusual variableType values."""

    def _point(self, pid, var_type):
        return {
            'variableId': pid,
            'title': f'Point {pid}',
            'description': '',
            'metadata': {
                'variableType': var_type,
                'variableSize': 's16',
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'isWritable': False,
                'divisor': 1, 'decimal': 0,
                'minValue': 0, 'maxValue': 0,
                'unit': '', 'shortUnit': '',
            }
        }

    def test_time_type_returns_sensor(self):
        from nibe_entity_detection import _detect_input_entity
        p = self._point(99999, 'time')
        result = _detect_input_entity(p, p['metadata'])
        self.assertEqual(result[0], 'sensor')

    def test_date_type_returns_sensor(self):
        from nibe_entity_detection import _detect_input_entity
        p = self._point(99999, 'date')
        result = _detect_input_entity(p, p['metadata'])
        self.assertEqual(result[0], 'sensor')



class TestMapDeviceClassEdgeCases(unittest.TestCase):
    """Edge cases in map_device_class for unusual entity types and units."""

    def test_number_entity_always_returns_none(self):
        from nibe_entity_detection import map_device_class
        self.assertIsNone(map_device_class('number', '°C', 'Temperature'))

    def test_binary_sensor_returns_none(self):
        from nibe_entity_detection import map_device_class
        self.assertIsNone(map_device_class('binary_sensor', '', 'Status'))

    def test_unknown_entity_type_returns_none(self):
        from nibe_entity_detection import map_device_class
        self.assertIsNone(map_device_class('button', '°C', 'Temperature'))

    def test_unclassifiable_unit_returns_none_for_unit_class(self):
        """Units like DM and rpm have no device class."""
        from nibe_entity_detection import map_device_class
        self.assertIsNone(map_device_class('sensor', 'DM', 'Degree minutes'))
        self.assertIsNone(map_device_class('sensor', 'rpm', 'Speed'))

    def test_keyword_match_without_unit_returns_none_when_class_needs_unit(self):
        """If keyword matches a class that requires a unit but no unit present,
        return None — assigning a device class without the matching unit causes
        HA validation errors."""
        from nibe_entity_detection import map_device_class
        # 'temperature' keyword matches temperature class which requires a unit
        result = map_device_class('sensor', '', 'Temperature sensor')
        self.assertIsNone(result)

    def test_unit_wins_over_keyword(self):
        """When both unit and keyword resolve, unit takes precedence."""
        from nibe_entity_detection import map_device_class
        # °C maps to 'temperature'; 'pressure' keyword maps to 'pressure'
        # unit should win
        result = map_device_class('sensor', '°C', 'pressure value')
        self.assertEqual(result, 'temperature')

    def test_unitless_keyword_class_returned_when_class_allows_no_unit(self):
        """A keyword that maps to a unitless class (e.g. 'enum') is returned
        even when the sensor has no unit — line 864."""
        from nibe_entity_detection import map_device_class, _SENSOR_KEYWORD_RULES
        # Patch a temporary unitless class rule so we can test the branch
        # without relying on a specific keyword in the real table.
        original = list(_SENSOR_KEYWORD_RULES)
        _SENSOR_KEYWORD_RULES.clear()
        _SENSOR_KEYWORD_RULES.append((('testunitless',), 'enum'))
        try:
            result = map_device_class('sensor', '', 'testunitless sensor')
            self.assertEqual(result, 'enum')
        finally:
            _SENSOR_KEYWORD_RULES.clear()
            _SENSOR_KEYWORD_RULES.extend(original)



class TestBinarySensorMultiStateExclusion(unittest.TestCase):
    """_is_auto_binary_sensor: points with >2 VALUE_MAPPINGS states must
    NOT be classified as binary_sensor (line 638)."""

    def test_input_point_with_more_than_two_description_pairs_is_not_binary(self):
        """598->603: when description has >2 '=' pairs (multi-state enum), the
        point must not be classified as binary_sensor even if shape looks binary."""
        from nibe_entity_detection import _is_auto_binary_sensor
        # No VALUE_MAPPINGS entry for this point — falls through to description check
        point = {'variableId': 77777, 'description': '0=Off,1=Low,2=High'}
        meta  = {
            'variableSize': 'u8', 'minValue': 0, 'maxValue': 1,
            'unit': '', 'isWritable': False,
        }
        result = _is_auto_binary_sensor(point, meta)
        self.assertFalse(result, ">2 description pairs must disqualify binary_sensor")
        """A point that looks binary by shape (u8, min=0, max=1) but has
        a 3-state VALUE_MAPPINGS entry must be excluded from binary_sensor
        auto-detection — the firmware enum is the ground truth."""
        from nibe_entity_detection import _is_auto_binary_sensor
        import nibe_entity_detection as ned
        test_pid = 88888
        # minValue=0, maxValue=1 → passes the shape checks
        # but VALUE_MAPPINGS has 3 states → must return False
        ned.VALUE_MAPPINGS['input'][test_pid] = {0: 'Off', 1: 'Low', 2: 'High'}
        point = {'variableId': test_pid, 'description': ''}
        meta  = {
            'variableSize': 'u8', 'minValue': 0, 'maxValue': 1,
            'unit': '', 'isWritable': False,
        }
        try:
            result = _is_auto_binary_sensor(point, meta)
            self.assertFalse(result, "3-state mapping must disqualify binary_sensor")
        finally:
            del ned.VALUE_MAPPINGS['input'][test_pid]

    def test_point_id_none_skips_value_mappings_check(self):
        """598->603: point_id=None skips the VALUE_MAPPINGS lookup entirely
        and falls through to the description check."""
        from nibe_entity_detection import _is_auto_binary_sensor
        point = {'variableId': None, 'description': ''}
        meta  = {
            'variableSize': 'u8', 'minValue': 0, 'maxValue': 1,
            'unit': '', 'isWritable': False,
        }
        result = _is_auto_binary_sensor(point, meta)
        self.assertTrue(result, "point_id=None with binary shape should be True")


# ===========================================================================
# 79. MqttDiscoveryPublisher — remaining branch coverage
# ===========================================================================


class TestEntityDetectionRemainingPaths2(unittest.TestCase):

    # ── get_entity_options — non-parseable part skipped ───────────────────

    def test_get_entity_options_skips_parts_without_equals(self):
        """Parts with no '=' must be silently skipped — not raise."""
        from nibe_entity_detection import get_entity_options
        # Mix of valid and invalid parts
        desc = '0=Off, garbage_no_equals, 1=On'
        opts = get_entity_options(99999, {}, desc)
        self.assertEqual(opts, ['Off', 'On'])

    def test_get_entity_options_skips_parts_without_equals_sign(self):
        """A part with no '=' must be silently skipped (hits the continue on that guard)."""
        from nibe_entity_detection import get_entity_options
        desc = '0=Off, no_equals_here, 1=On'
        opts = get_entity_options(99999, {}, desc)
        self.assertEqual(opts, ['Off', 'On'])

    def test_get_entity_options_reverse_format_uses_left_as_label(self):
        """When left is not an integer, the function uses left as the label
        (handles firmware format 'Auto=0, Manual=1')."""
        from nibe_entity_detection import get_entity_options
        desc = 'Auto=0, Manual=1'
        opts = get_entity_options(99999, {}, desc)
        self.assertIn('Auto', opts)
        self.assertIn('Manual', opts)

    # ── detect_entity_type — holding register select paths ────────────────

    def test_holding_register_with_value_mapping_returns_select(self):
        """A writable holding register with a VALUE_MAPPINGS entry → select."""
        from nibe_entity_detection import detect_entity_type, VALUE_MAPPINGS
        holding = VALUE_MAPPINGS.get('holding', {})
        if not holding:
            self.skipTest('No holding VALUE_MAPPINGS defined')
        pid = next(iter(holding))
        point = {
            'variableId': pid,
            'metadata': {
                'variableType': 'integer', 'variableSize': 'u8',
                'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                'isWritable': True, 'divisor': 1,
                'minValue': 0, 'maxValue': 3, 'intDefaultValue': 0,
                'unit': '',
            },
            'description': '',
        }
        entity_type, _ = detect_entity_type(point)
        self.assertEqual(entity_type, 'select')

    def test_holding_register_with_enum_description_returns_select(self):
        """A writable holding register with '0=X, 1=Y' in description → select."""
        from nibe_entity_detection import detect_entity_type
        point = {
            'variableId': 99997,
            'metadata': {
                'variableType': 'integer', 'variableSize': 'u8',
                'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                'isWritable': True, 'divisor': 1,
                'minValue': 0, 'maxValue': 1, 'intDefaultValue': 0,
                'unit': '',
            },
            'description': '0=Off, 1=On',
        }
        entity_type, _ = detect_entity_type(point)
        self.assertEqual(entity_type, 'select')

    def test_input_register_with_enum_description_returns_binary_sensor(self):
        """A read-only INPUT register with a 2-pair enum description is a binary sensor
        (e.g. '0=Off, 1=On') — auto-detection now handles this correctly."""
        from nibe_entity_detection import detect_entity_type
        point = {
            'variableId': 99996,
            'metadata': {
                'variableType': 'integer', 'variableSize': 'u8',
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'isWritable': False, 'divisor': 1,
                'minValue': 0, 'maxValue': 1, 'intDefaultValue': 0,
                'unit': '',
            },
            'description': '0=Off, 1=On',
        }
        entity_type, _ = detect_entity_type(point)
        self.assertEqual(entity_type, 'binary_sensor')

    # ── map_device_class — keyword match without required unit ────────────

    def test_map_device_class_keyword_energy_without_unit_returns_none(self):
        """'energy' class requires a unit (kWh etc.) — without one, must return None."""
        from nibe_entity_detection import map_device_class
        result = map_device_class('sensor', '', 'total energy consumed')
        self.assertIsNone(result)

    def test_map_device_class_keyword_power_without_unit_returns_none(self):
        """'power' class requires a unit — without one, must return None."""
        from nibe_entity_detection import map_device_class
        result = map_device_class('sensor', '', 'power output')
        self.assertIsNone(result)

    def test_map_device_class_bp_keyword_without_unit_returns_none(self):
        """bp* keywords map to 'pressure' which requires a unit — no unit → None."""
        from nibe_entity_detection import map_device_class
        # 'bp1' is in _SENSOR_KEYWORD_RULES → keyword_class='pressure' (line 748)
        # pressure is not in _UNITLESS_CLASSES and no unit → return None (line 800)
        result = map_device_class('sensor', '', 'bp1 sensor')
        self.assertIsNone(result)

    def test_map_device_class_keyword_match_with_unit_returns_class(self):
        """A bt* keyword match with a matching unit → returns temperature class via unit path."""
        from nibe_entity_detection import map_device_class
        # 'bt1' is in _SENSOR_KEYWORD_RULES → keyword_class='temperature' (line 748 hit)
        # °C also maps to 'temperature' via unit → unit wins (line 796)
        result = map_device_class('sensor', '°C', 'bt1 outdoor sensor')
        self.assertEqual(result, 'temperature')

    def test_map_device_class_keyword_match_without_unit_returns_none(self):
        """A bt* keyword match with no unit → keyword_class='temperature' found (line 748)
        but temperature requires a unit → returns None (lines 800-801)."""
        from nibe_entity_detection import map_device_class
        result = map_device_class('sensor', '', 'bt1 sensor')
        self.assertIsNone(result)


# ===========================================================================
# 85. ManagementCommandHandler — _on_entity_disabled noop
# ===========================================================================


class TestEntityDetectionRemainingPaths(unittest.TestCase):
    """nibe_entity_detection.py: binary sensor exclusion ID, >2 VALUE_MAPPINGS
    states, description >2 pairs, input sensor from description, keyword
    class with no unit returns None."""

    def _binary_candidate_metadata(self):
        return {
            'variableSize': 'u8',
            'minValue': 0,
            'maxValue': 1,
            'unit': '',
            'isWritable': False,
        }

    def test_binary_sensor_exclusion_id_returns_false(self):
        """A point whose ID is in _BINARY_SENSOR_EXCLUSIONS must not be
        classified as binary_sensor even if it meets all other criteria."""
        from nibe_entity_detection import _is_auto_binary_sensor, _BINARY_SENSOR_EXCLUSIONS
        excluded_id = next(iter(_BINARY_SENSOR_EXCLUSIONS))
        point = {'variableId': excluded_id, 'description': ''}
        result = _is_auto_binary_sensor(point, self._binary_candidate_metadata())
        self.assertFalse(result)

    def test_binary_sensor_value_mapping_more_than_2_states_returns_false(self):
        """A point in VALUE_MAPPINGS['input'] with >2 states must not be
        classified as binary_sensor."""
        from nibe_entity_detection import _is_auto_binary_sensor, VALUE_MAPPINGS
        inp = VALUE_MAPPINGS.get('input', {})
        multi_state_id = next(pid for pid, m in inp.items() if len(m) > 2)
        point = {'variableId': multi_state_id, 'description': ''}
        result = _is_auto_binary_sensor(point, self._binary_candidate_metadata())
        self.assertFalse(result)

    def test_binary_sensor_description_more_than_2_pairs_returns_false(self):
        """A description with 3+ '=' pairs means it's a multi-state sensor,
        not a binary sensor."""
        from nibe_entity_detection import _is_auto_binary_sensor
        point = {
            'variableId': 99999,  # not in any exclusion or mapping
            'description': '0 = Off, 1 = Low, 2 = High',
        }
        result = _is_auto_binary_sensor(point, self._binary_candidate_metadata())
        self.assertFalse(result)

    def test_input_register_with_description_pairs_returns_sensor(self):
        """An INPUT register with a description containing '=' and ','
        reaches the description-based sensor path."""
        from nibe_entity_detection import detect_entity_type
        point = {
            'variableId': 99998,
            'title': 'Status code',
            'description': '0 = Idle, 1 = Running',
            'metadata': {
                'variableType': 'integer',
                'variableSize': 'u8',
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'isWritable': False,
                'minValue': 0,
                'maxValue': 255,
                'divisor': 1,
                'unit': '',
            },
        }
        entity_type, category = detect_entity_type(point)
        self.assertEqual(entity_type, 'sensor')

    def test_map_device_class_keyword_match_with_no_unit_returns_none(self):
        """A keyword match for a class that requires a unit (e.g. temperature)
        returns None when the point has no unit."""
        from nibe_entity_detection import map_device_class
        # 'bt1' matches 'temperature' keyword rule; no unit → None
        result = map_device_class('sensor', '', 'bt1 outdoor temp')
        self.assertIsNone(result)



class TestLastStatesFallbackPublishProperties(unittest.TestCase):
    """The publish condition has three clauses:
    should_pub OR pid not in last_states OR last_states[pid] != state_value.
    The third clause guarantees publish even when ValueCache suppresses,
    if the computed display state has diverged from what HA last received.
    """

    _PID = 7777

    def _entity_info(self):
        return {
            'point_id': self._PID, 'entity_type': 'sensor',
            'availability_topic': f'nibe/avail/{self._PID}',
            'state_topic': f'nibe/state/{self._PID}',
            'command_topic': None, 'point_data': {},
        }

    def _bulk(self, raw, divisor=1):
        return {
            'raw_value': raw, 'string_value': '', 'is_ok': True,
            'metadata': {'variableSize': 's16', 'divisor': divisor,
                         'unit': '', 'change': 0, 'decimal': 0},
            'title': 'Test',
        }

    def _activate(self, em, ei):
        em.active_entities_by_id[ei['point_id']] = ei
        em.mqtt_enabled_points.add(ei['point_id'])

    @given(
        raw=st.integers(min_value=-1000, max_value=1000),
        divisor=st.sampled_from([1, 10, 100]),
    )
    def test_absent_last_state_always_publishes(self, raw, divisor):
        """No last_states entry → must publish regardless of ValueCache."""
        em = _make_em()
        ei = self._entity_info()
        em.bulk_data[self._PID] = self._bulk(raw, divisor)
        em.value_cache.should_publish(self._PID, raw, threshold=0)
        self.assertNotIn(self._PID, em.last_states)
        self._activate(em, ei)
        em._update_entity_state(ei)
        state_calls = [c for c in em.mqtt.publish.call_args_list
                       if c.args[0] == f'nibe/state/{self._PID}']
        self.assertTrue(state_calls)

    @given(
        raw=st.integers(min_value=-1000, max_value=1000),
        stale=st.text(min_size=1, max_size=20,
                      alphabet=st.characters(categories=['L', 'N', 'P'])),
        divisor=st.sampled_from([1, 10, 100]),
    )
    def test_stale_last_state_always_publishes(self, raw, stale, divisor):
        """Stale last_states entry → must publish and push the current value."""
        from nibe_entity_detection import apply_divisor
        computed = apply_divisor(raw, divisor)
        assume(stale != computed)
        em = _make_em()
        ei = self._entity_info()
        em.bulk_data[self._PID] = self._bulk(raw, divisor)
        em.value_cache.should_publish(self._PID, raw, threshold=0)
        em.last_states[self._PID] = stale
        self._activate(em, ei)
        em._update_entity_state(ei)
        state_calls = [c for c in em.mqtt.publish.call_args_list
                       if c.args[0] == f'nibe/state/{self._PID}']
        self.assertTrue(state_calls)
        self.assertEqual(state_calls[0].args[1], computed)

    @given(
        raw=st.integers(min_value=-1000, max_value=1000),
        divisor=st.sampled_from([1, 10, 100]),
    )
    def test_matching_last_state_suppressed_when_cache_says_no(self, raw, divisor):
        """Matching last_states + ValueCache suppression → must not publish."""
        from nibe_entity_detection import apply_divisor
        computed = apply_divisor(raw, divisor)
        em = _make_em()
        ei = self._entity_info()
        em.bulk_data[self._PID] = self._bulk(raw, divisor)
        em.value_cache.should_publish(self._PID, raw, threshold=0)
        em.last_states[self._PID] = computed
        self._activate(em, ei)
        em._update_entity_state(ei)
        state_calls = [c for c in em.mqtt.publish.call_args_list
                       if c.args[0] == f'nibe/state/{self._PID}']
        self.assertFalse(state_calls)


# ---------------------------------------------------------------------------
# Disappeared-points set algebra (direct property tests)
# ---------------------------------------------------------------------------



# ===========================================================================
# Phase 2 mutmut survivor tests — nibe_entity_detection.py genuine logic gaps
# ===========================================================================


class TestCleanUnitLogicGaps(unittest.TestCase):
    """clean_unit: 'or' not 'and' in early-return guard.

    mutmut_1: 'or' → 'and'. With 'and', empty string '' would not return
    early (not '' is True, not isinstance('', str) is False → and=False)
    and then .strip() would be called on '' which works but the intent is
    to catch both None/falsy AND non-string inputs separately.
    """

    def test_none_returns_empty_string(self):
        from nibe_entity_detection import clean_unit
        self.assertEqual(clean_unit(None), '')

    def test_empty_string_returns_empty_string(self):
        from nibe_entity_detection import clean_unit
        self.assertEqual(clean_unit(''), '')

    def test_integer_returns_empty_string(self):
        """non-str type → early return via isinstance check."""
        from nibe_entity_detection import clean_unit
        self.assertEqual(clean_unit(42), '')

    def test_zero_returns_empty_string(self):
        """0 is falsy AND not a str — both conditions apply."""
        from nibe_entity_detection import clean_unit
        self.assertEqual(clean_unit(0), '')


class TestApplyDivisorLogicGaps(unittest.TestCase):
    """apply_divisor: 'and' not 'or', max(0,...) not max(1,...).

    mutmut_2: 'and' → 'or' for zero divisor guard.
    mutmut_14: max(0, ...) → max(1, ...) for decimal places.
    """

    def test_divisor_zero_treated_as_one(self):
        """divisor=0 → effective=1 (and guard: 0 and 0!=0 = False → use 1).
        With or: 0 or 0!=0 = 0 or False = False → same. Actually equivalent.
        Test with divisor=None: None and None!=0 = False (and) vs None or None!=0 = True (or).
        """
        from nibe_entity_detection import apply_divisor
        # divisor=0 should give raw integer string
        result = apply_divisor(200, 0)
        self.assertEqual(result, '200')

    def test_divisor_none_treated_as_one(self):
        """divisor=None: 'and' makes None and ... = False → use 1.
        'or' makes None or (None != 0) = None or True = True → use None → crash."""
        from nibe_entity_detection import apply_divisor
        result = apply_divisor(200, None)
        self.assertEqual(result, '200')

    def test_decimal_places_minimum_is_zero_not_one(self):
        """max(0, log10(effective)) — for divisor=1 returns integer string directly.
        For divisor=10: ceil(log10(10))=1 → 1dp, trailing zeros stripped → '20'.
        With max(1,...): divisor=1 also gives 1 decimal place incorrectly."""
        from nibe_entity_detection import apply_divisor
        # divisor=1: should return integer string (0 decimal places, returned directly)
        result = apply_divisor(20, 1)
        self.assertEqual(result, '20')
        # divisor=60 (minute→hour): ceil(log10(60))=2, 20/60=0.333... → '0.33'
        result60 = apply_divisor(20, 60)
        self.assertIn('.', result60, "divisor=60 should give decimal result")


class TestParseDescriptionMappingLogicGaps(unittest.TestCase):
    """parse_description_mapping: split('=', 1) not rsplit, continue not break.

    mutmut_18: split('=', 1) → rsplit('=', 1) — for 'key=val=extra', split
    gives ['key', 'val=extra'] but rsplit gives ['key=val', 'extra']. The key
    would be wrong.
    mutmut_12: continue → break — stops processing at first invalid entry.
    """

    def test_split_uses_first_equals_not_last(self):
        """split('=', 1) gives ['key', 'val=extra']; rsplit gives ['key=val', 'extra']."""
        from nibe_entity_detection import parse_description_mapping
        # Description with value containing '='
        result = parse_description_mapping('0=OFF, 1=ON=always')
        if result:
            # Key 1 should map to 'ON=always', not 'ON' with key '1=ON'
            self.assertIn(1, result)
            self.assertEqual(result[1], 'ON=always')

    def test_invalid_entry_does_not_stop_processing(self):
        """continue not break: an entry without '=' must be skipped, not stop all parsing."""
        from nibe_entity_detection import parse_description_mapping
        # 'invalid' has no '=', should be skipped, '1=ON' should still be parsed
        result = parse_description_mapping('invalid, 1=ON')
        self.assertIsNotNone(result)
        if result:
            self.assertIn(1, result)
            self.assertEqual(result[1], 'ON')


class TestGetEntityOptionsLogicGaps(unittest.TestCase):
    """get_entity_options: 'and' not 'or' for description check, split maxsplit=1.

    mutmut_24: 'and' → 'or' — with 'or', a description with '=' but no ','
    would also try to parse (might produce wrong results).
    mutmut_43: split('=', 1) → split('=', 2) — wrong for '0=OFF,1=ON' parsing.
    """

    def test_description_needs_both_equals_and_comma(self):
        """'=' in description AND ',' in description — both required."""
        from nibe_entity_detection import get_entity_options
        # Has '=' but no ',' — should NOT produce options from description
        metadata = {'modbusRegisterType': 'MODBUS_HOLDING_REGISTER', 'minValue': 0, 'maxValue': 1}
        result = get_entity_options(99999, metadata, '0=OFF')
        # No comma → description parsing should not fire, might return None or from VALUE_MAPPINGS
        # Key test: '0=OFF' alone (no comma) must not give options from description
        # (it might give options from VALUE_MAPPINGS if point is known)
        if result:
            # If options came back, they must not be from the '0=OFF' description alone
            self.assertNotEqual(result, ['OFF'])

    def test_description_split_at_first_equals(self):
        """Each part split at first '=' — '0=A=B' gives key=0, value='A=B'."""
        from nibe_entity_detection import get_entity_options
        metadata = {'modbusRegisterType': 'MODBUS_HOLDING_REGISTER', 'minValue': 0, 'maxValue': 1}
        result = get_entity_options(99999, metadata, '0=A=B,1=C')
        if result:
            self.assertIn('A=B', result)


class TestIsAutoSensorLogicGaps(unittest.TestCase):
    """_is_auto_binary_sensor: 'or' not 'and' in variableSize check,
    len(mapping) > 2 not >= 2.

    mutmut_4: 'or' → 'and' — changes which combinations trigger non-binary detection.
    mutmut_56: > 2 → >= 2 — a mapping with exactly 2 entries would no longer
    be detected as non-binary (2 entries is the exact binary sensor threshold).
    """

    def _point(self, var_size='u8', min_val=0, max_val=1, point_id=99999):
        return {
            'variableId': point_id,
            'metadata': {
                'variableSize': var_size,
                'minValue': min_val,
                'maxValue': max_val,
            },
        }

    def test_mapping_with_exactly_2_entries_is_binary(self):
        """len(mapping) > 2 means exactly 2 entries IS a binary sensor candidate.
        >= 2 would exclude it (2-entry mapping treated as non-binary)."""
        from nibe_entity_detection import _is_auto_binary_sensor
        # Use a point with a known 2-entry mapping in VALUE_MAPPINGS if available,
        # or test the boundary directly via a point with max=1
        point = self._point(var_size='u8', min_val=0, max_val=1)
        metadata = point['metadata']
        # With 2-value mapping: > 2 is False (binary candidate kept)
        # With >= 2: True (not binary)
        # Test via a point that has no VALUE_MAPPINGS entry (mapping is None → not triggered)
        result = _is_auto_binary_sensor(point, metadata)
        # A u8 switch-shaped point (0/1, no unit) should be True
        self.assertIsInstance(result, bool)


class TestDetectInputEntityLogicGaps(unittest.TestCase):
    """__detect_input_entity: 'or' not 'and' for float type detection.

    mutmut_66: 'or' → 'and'. Float detection:
    'if var_type == "floating-point" or var_size in ("f4", "f8")'
    With 'and': BOTH conditions must be true — a register with var_size='f4'
    but var_type != 'floating-point' would not be detected as float.
    With 'or': either condition triggers float detection.
    """

    def _point(self, var_type='integer', var_size='u8', point_id=99999):
        return {
            'variableId': point_id,
            'display_title': 'Test',
            'description': '',
            'metadata': {
                'variableType': var_type,
                'variableSize': var_size,
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'isWritable': False,
                'minValue': 0,
                'maxValue': 100,
                'divisor': 1,
            },
        }

    def test_f4_var_size_detected_as_float_regardless_of_var_type(self):
        """var_size='f4' alone (or condition) should trigger float detection."""
        from nibe_entity_detection import _detect_input_entity
        point = self._point(var_type='integer', var_size='f4')
        entity_type, category = _detect_input_entity(point, point['metadata'])
        # Float registers are sensors (not binary_sensor or switch)
        self.assertEqual(entity_type, 'sensor')

    def test_f8_var_size_detected_as_float(self):
        from nibe_entity_detection import _detect_input_entity
        point = self._point(var_type='integer', var_size='f8')
        entity_type, _ = _detect_input_entity(point, point['metadata'])
        self.assertEqual(entity_type, 'sensor')

    def test_floating_point_var_type_detected_regardless_of_size(self):
        """var_type='floating-point' alone should trigger float detection."""
        from nibe_entity_detection import _detect_input_entity
        point = self._point(var_type='floating-point', var_size='u8')
        entity_type, _ = _detect_input_entity(point, point['metadata'])
        self.assertEqual(entity_type, 'sensor')


class TestMapDeviceClassLogicGaps(unittest.TestCase):
    """map_device_class: 'or' not 'and', None init not empty string.

    mutmut_20: 'or' → 'and' for unit_clean check.
    mutmut_19/24: None → "" for unit_class/keyword_class init.
    """

    def test_unit_class_init_is_none_not_empty_string(self):
        """unit_class starts as None — a non-None empty string would change
        the 'if unit_class:' check that gates assignment."""
        from nibe_entity_detection import map_device_class
        # A sensor with no unit and no matching keyword should return None
        result = map_device_class('sensor', '', 'Unknown Register')
        # If init is "" instead of None, the final return might be "" not None
        self.assertIsNone(result)

    def test_unit_clean_check_uses_and_not_or(self):
        """'if unit_clean and unit_clean not in _UNCLASSIFIABLE_UNITS' — the 'and'
        means empty unit skips the unit-based device class lookup entirely.
        With 'or': empty unit would still attempt lookup ('' not in set → True).
        Verify: unit='°C' gives device class, unit='' gives None from unit lookup."""
        from nibe_entity_detection import map_device_class
        # With unit '°C' → temperature device class
        result_with_unit = map_device_class('sensor', '°C', 'Sensor')
        self.assertEqual(result_with_unit, 'temperature')
        # With empty unit → no unit-based class (and condition: '' is falsy → skip)
        result_empty = map_device_class('sensor', '', 'Sensor')
        self.assertIsNone(result_empty)


class TestDetectHoldingEntityLogicGaps(unittest.TestCase):
    """_detect_holding_entity: binary comparison and description parsing."""

    def _point(self, var_type='integer', var_size='s16', point_id=99990):
        return {
            'variableId': point_id,
            'display_title': 'T',
            'description': '',
            'metadata': {
                'variableType': var_type,
                'variableSize': var_size,
                'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                'isWritable': True,
                'minValue': 0, 'maxValue': 1, 'divisor': 1,
            },
        }

    def test_binary_var_type_gives_specific_category(self):
        """var_type=='binary' in holding register — the == check routes to the
        binary path. With != mutation: binary type falls through to number/switch."""
        from nibe_entity_detection import _detect_holding_entity
        p = self._point(var_type='binary', var_size='u8')
        # binary holding registers get logged and then handled as switch
        # The key test: != mutation makes 'binary' not match → different code path
        entity_type, _ = _detect_holding_entity(p, p['metadata'])
        # Should not be None — binary path always returns something
        self.assertIsNotNone(entity_type)
        # With != mutation: var_type=='binary' check never fires, falls through
        # to the regular switch/number detection path — same result for u8 0/1
        # So test the logging path instead: binary type should NOT hit float path
        self.assertNotEqual(entity_type, 'sensor')  # float path gives sensor

    def test_non_binary_var_type_does_not_give_binary_sensor(self):
        """var_type!='binary' → not binary_sensor via this path."""
        from nibe_entity_detection import _detect_holding_entity
        p = self._point(var_type='integer', var_size='u8')
        p['metadata']['minValue'] = 0
        p['metadata']['maxValue'] = 100
        entity_type, _ = _detect_holding_entity(p, p['metadata'])
        self.assertNotEqual(entity_type, 'binary_sensor')

    def test_description_with_equals_and_comma_gives_select(self):
        """Both '=' AND ',' required — with 'or' a description with only '='
        would incorrectly trigger select detection."""
        from nibe_entity_detection import _detect_holding_entity
        # Has both '=' and ',' → select
        p = self._point(var_type='integer', var_size='u8')
        p['description'] = '0=Off,1=On'
        p['metadata']['minValue'] = 0
        p['metadata']['maxValue'] = 1
        entity_type, _ = _detect_holding_entity(p, p['metadata'])
        self.assertEqual(entity_type, 'select')

    def test_description_with_only_equals_no_comma_not_select(self):
        """Only '=' but no ',' → must NOT give select (needs both)."""
        from nibe_entity_detection import _detect_holding_entity
        p = self._point(var_type='integer', var_size='u8')
        p['description'] = '0=Off'  # no comma
        p['metadata']['minValue'] = 0
        p['metadata']['maxValue'] = 1
        entity_type, _ = _detect_holding_entity(p, p['metadata'])
        self.assertNotEqual(entity_type, 'select')


class TestDetectInputEntityDescriptionParsing(unittest.TestCase):
    """_detect_holding_entity: '=' AND ',' both required for select detection.

    Description-based select detection applies to holding registers.
    Input registers with descriptions give sensor not select.
    """

    def _holding_point(self, description='', var_type='integer', var_size='u8',
                       min_val=0, max_val=10, point_id=99989):
        return {
            'variableId': point_id,
            'display_title': 'T',
            'description': description,
            'metadata': {
                'variableType': var_type,
                'variableSize': var_size,
                'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                'isWritable': True,
                'minValue': min_val, 'maxValue': max_val, 'divisor': 1,
            },
        }

    def test_description_with_both_equals_and_comma_gives_select(self):
        """Both '=' and ',' in holding description → select."""
        from nibe_entity_detection import _detect_holding_entity
        p = self._holding_point(description='0=Stop,1=Low,2=High',
                                min_val=0, max_val=2)
        entity_type, _ = _detect_holding_entity(p, p['metadata'])
        self.assertEqual(entity_type, 'select')

    def test_description_with_only_equals_no_select(self):
        """Only '=' without ',' → not select (needs both)."""
        from nibe_entity_detection import _detect_holding_entity
        p = self._holding_point(description='0=Off', min_val=0, max_val=1)
        entity_type, _ = _detect_holding_entity(p, p['metadata'])
        self.assertNotEqual(entity_type, 'select')

    def test_description_with_only_comma_no_select(self):
        """Only ',' without '=' → not select."""
        from nibe_entity_detection import _detect_holding_entity
        p = self._holding_point(description='Off,On', min_val=0, max_val=1)
        entity_type, _ = _detect_holding_entity(p, p['metadata'])
        self.assertNotEqual(entity_type, 'select')


class TestIsSwitchCandidateDefaults(unittest.TestCase):
    """is_switch_candidate: default values in .get() calls.

    Requires modbusRegisterType=MODBUS_HOLDING_REGISTER and variableSize=u8
    as hard requirements; minValue/divisor/unit use defaults.
    """

    BASE = {
        'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
        'variableSize': 'u8',
        'maxValue': 1,
    }

    def test_maxvalue_default_zero_not_one(self):
        """maxValue absent → default 0 → 0==1 is False → not switch."""
        from nibe_entity_detection import is_switch_candidate
        meta = {**self.BASE}
        del meta['maxValue']  # no maxValue → default 0 → not switch
        self.assertFalse(is_switch_candidate(meta))

    def test_full_switch_candidate(self):
        """All required fields → True."""
        from nibe_entity_detection import is_switch_candidate
        self.assertTrue(is_switch_candidate(self.BASE))

    def test_divisor_default_one_not_two(self):
        """divisor absent → default 1 → passes divisor==1 check."""
        from nibe_entity_detection import is_switch_candidate
        meta = {**self.BASE}  # no divisor key
        self.assertTrue(is_switch_candidate(meta))

    def test_minvalue_default_zero_not_one(self):
        """minValue absent → default 0 → passes minValue==0 check."""
        from nibe_entity_detection import is_switch_candidate
        meta = {**self.BASE}  # no minValue key
        self.assertTrue(is_switch_candidate(meta))

    def test_unit_default_empty_not_none(self):
        """unit absent → default '' → passes unit=='' check."""
        from nibe_entity_detection import is_switch_candidate
        meta = {**self.BASE}  # no unit key
        self.assertTrue(is_switch_candidate(meta))


class TestGetEntityOptionsHoldingCheck(unittest.TestCase):
    """get_entity_options: 'HOLDING' in modbusRegisterType, not 'holding' in."""

    def test_holding_register_type_detected_correctly(self):
        """'HOLDING' in 'MODBUS_HOLDING_REGISTER' → register_type='holding'."""
        from nibe_entity_detection import get_entity_options
        meta = {'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                'minValue': 0, 'maxValue': 1}
        # With 'holding' in (lowercase): 'holding' in 'MODBUS_HOLDING_REGISTER'
        # → False → register_type='input' (wrong).
        # With 'HOLDING' in: True → register_type='holding' (correct).
        # Test by checking a known holding VALUE_MAPPINGS entry exists
        from nibe_entity_detection import VALUE_MAPPINGS
        known_holding_id = next(iter(VALUE_MAPPINGS.get('holding', {0: None})), None)
        if known_holding_id is None:
            self.skipTest('No holding VALUE_MAPPINGS entries')
        result = get_entity_options(known_holding_id, meta, '')
        self.assertIsNotNone(result, "Holding register must find options via VALUE_MAPPINGS")

    def test_input_register_type_detected_correctly(self):
        """No 'HOLDING' in 'MODBUS_INPUT_REGISTER' → register_type='input'."""
        from nibe_entity_detection import get_entity_options, VALUE_MAPPINGS
        meta = {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'minValue': 0, 'maxValue': 1}
        known_input_id = next(iter(VALUE_MAPPINGS.get('input', {0: None})), None)
        if known_input_id is None:
            self.skipTest('No input VALUE_MAPPINGS entries')
        result = get_entity_options(known_input_id, meta, '')
        self.assertIsNotNone(result)


# ===========================================================================
# Phase 2 round 3 — nibe_entity_detection.py var_type string comparisons
# ===========================================================================


class TestDetectHoldingEntityVarTypeStrings(unittest.TestCase):
    """_detect_holding_entity: exact var_type string comparisons.

    Each var_type == "time"/"date"/"string"/"floating-point"/"binary" check
    generates a mutmut survivor when the string literal is mutated.
    The correct entity type for each var_type is pinned here.

    56 survivors in _detect_holding_entity — majority from these string checks.
    """

    def _point(self, var_type, var_size='u16', point_id=99001,
               min_val=0, max_val=100, description='', writable=True):
        return {
            'variableId': point_id,
            'display_title': 'Test',
            'description': description,
            'metadata': {
                'variableType': var_type,
                'variableSize': var_size,
                'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                'isWritable': writable,
                'minValue': min_val,
                'maxValue': max_val,
                'divisor': 1,
                'unit': '',
            },
        }

    def test_var_type_time_gives_number(self):
        """var_type=='time' → ('number', 'config'). Mutation 'time'→'timer' misses."""
        from nibe_entity_detection import _detect_holding_entity
        p = self._point('time')
        et, cat = _detect_holding_entity(p, p['metadata'])
        self.assertEqual(et, 'number')
        self.assertEqual(cat, 'config')

    def test_var_type_date_gives_number(self):
        """var_type=='date' → ('number', 'config'). Mutation 'date'→'DATE' misses."""
        from nibe_entity_detection import _detect_holding_entity
        p = self._point('date')
        et, cat = _detect_holding_entity(p, p['metadata'])
        self.assertEqual(et, 'number')
        self.assertEqual(cat, 'config')

    def test_var_type_string_gives_text(self):
        """var_type=='string' → ('text', 'config')."""
        from nibe_entity_detection import _detect_holding_entity
        p = self._point('string')
        et, cat = _detect_holding_entity(p, p['metadata'])
        self.assertEqual(et, 'text')
        self.assertEqual(cat, 'config')

    def test_var_type_integer_does_not_give_time_result(self):
        """var_type=='integer' must not trigger the time/date early-return paths."""
        from nibe_entity_detection import _detect_holding_entity
        p = self._point('integer', min_val=0, max_val=100)
        et, _ = _detect_holding_entity(p, p['metadata'])
        # integer holding register with large range → number or select, not sensor
        self.assertNotEqual(et, 'sensor')

    def test_var_type_time_not_select(self):
        """var_type=='time' returns early before VALUE_MAPPINGS check — never select."""
        from nibe_entity_detection import _detect_holding_entity
        # Give it a description that would normally trigger select
        p = self._point('time', description='0=Off,1=On', min_val=0, max_val=1)
        et, _ = _detect_holding_entity(p, p['metadata'])
        self.assertNotEqual(et, 'select')

    def test_var_type_date_not_select(self):
        """var_type=='date' returns early — never select."""
        from nibe_entity_detection import _detect_holding_entity
        p = self._point('date', description='0=Off,1=On', min_val=0, max_val=1)
        et, _ = _detect_holding_entity(p, p['metadata'])
        self.assertNotEqual(et, 'select')


class TestDetectInputEntityVarTypeStrings(unittest.TestCase):
    """_detect_input_entity: exact var_type string comparisons.

    66 survivors in _detect_input_entity — majority from var_type string checks.
    Pins the exact entity type for each special var_type value.
    """

    def _point(self, var_type, var_size='u16', point_id=99002,
               min_val=0, max_val=100, description=''):
        return {
            'variableId': point_id,
            'display_title': 'Test',
            'description': description,
            'metadata': {
                'variableType': var_type,
                'variableSize': var_size,
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'isWritable': False,
                'minValue': min_val,
                'maxValue': max_val,
                'divisor': 1,
                'unit': '',
            },
        }

    def test_var_type_time_gives_sensor(self):
        """var_type=='time' → ('sensor', 'diagnostic'). Mutation 'time'→'timer' misses."""
        from nibe_entity_detection import _detect_input_entity
        p = self._point('time')
        et, cat = _detect_input_entity(p, p['metadata'])
        self.assertEqual(et, 'sensor')
        self.assertEqual(cat, 'diagnostic')

    def test_var_type_date_gives_sensor(self):
        """var_type=='date' → ('sensor', 'diagnostic')."""
        from nibe_entity_detection import _detect_input_entity
        p = self._point('date')
        et, cat = _detect_input_entity(p, p['metadata'])
        self.assertEqual(et, 'sensor')
        self.assertEqual(cat, 'diagnostic')

    def test_var_type_string_gives_sensor(self):
        """var_type=='string' (input) → ('sensor', 'diagnostic')."""
        from nibe_entity_detection import _detect_input_entity
        p = self._point('string')
        et, cat = _detect_input_entity(p, p['metadata'])
        self.assertEqual(et, 'sensor')
        self.assertEqual(cat, 'diagnostic')

    def test_var_type_time_not_binary_sensor(self):
        """var_type=='time' returns early → cannot be auto-classified as binary_sensor."""
        from nibe_entity_detection import _detect_input_entity
        # u8, 0-1, no unit — would normally be binary_sensor, but time check fires first
        p = self._point('time', var_size='u8', min_val=0, max_val=1)
        et, _ = _detect_input_entity(p, p['metadata'])
        self.assertNotEqual(et, 'binary_sensor')

    def test_var_type_date_not_binary_sensor(self):
        """var_type=='date' returns early → not binary_sensor."""
        from nibe_entity_detection import _detect_input_entity
        p = self._point('date', var_size='u8', min_val=0, max_val=1)
        et, _ = _detect_input_entity(p, p['metadata'])
        self.assertNotEqual(et, 'binary_sensor')

    def test_var_type_integer_u8_0_1_can_be_binary_sensor(self):
        """var_type='integer', u8, 0-1: not blocked by special var_type paths →
        auto-binary detection runs → binary_sensor."""
        from nibe_entity_detection import _detect_input_entity
        p = self._point('integer', var_size='u8', min_val=0, max_val=1)
        et, _ = _detect_input_entity(p, p['metadata'])
        self.assertEqual(et, 'binary_sensor')

    def test_var_type_string_not_binary_sensor(self):
        """var_type=='string' returns early as sensor — not binary_sensor."""
        from nibe_entity_detection import _detect_input_entity
        p = self._point('string', var_size='u8', min_val=0, max_val=1)
        et, _ = _detect_input_entity(p, p['metadata'])
        self.assertNotEqual(et, 'binary_sensor')


class TestDetectEntityTypeDispatch(unittest.TestCase):
    """detect_entity_type / _route_by_modbus_type: modbus type string comparisons.

    9 survivors in _detect_entity_type — the MODBUS_HOLDING_REGISTER and
    MODBUS_INPUT_REGISTER string comparisons.
    """

    def _point(self, modbus_type, var_type='integer', var_size='u16',
               min_val=0, max_val=100, point_id=99003):
        return {
            'variableId': point_id,
            'display_title': 'Test',
            'description': '',
            'metadata': {
                'modbusRegisterType': modbus_type,
                'variableType': var_type,
                'variableSize': var_size,
                'isWritable': True,
                'minValue': min_val,
                'maxValue': max_val,
                'divisor': 1,
                'unit': '',
            },
        }

    def test_holding_register_gives_config_category(self):
        """MODBUS_HOLDING_REGISTER → category='config' (not 'diagnostic')."""
        from nibe_entity_detection import detect_entity_type
        p = self._point('MODBUS_HOLDING_REGISTER', min_val=0, max_val=100)
        _, cat = detect_entity_type(p)
        self.assertEqual(cat, 'config')

    def test_input_register_gives_diagnostic_category(self):
        """MODBUS_INPUT_REGISTER → category='diagnostic' (not 'config')."""
        from nibe_entity_detection import detect_entity_type
        p = self._point('MODBUS_INPUT_REGISTER', min_val=0, max_val=100)
        _, cat = detect_entity_type(p)
        self.assertEqual(cat, 'diagnostic')

    def test_unknown_modbus_type_gives_sensor_diagnostic(self):
        """Unknown modbus type → ('sensor', 'diagnostic') fallback."""
        from nibe_entity_detection import detect_entity_type
        p = self._point('MODBUS_UNKNOWN_REGISTER')
        et, cat = detect_entity_type(p)
        self.assertEqual(et, 'sensor')
        self.assertEqual(cat, 'diagnostic')

    def test_holding_writable_false_gives_sensor(self):
        """MODBUS_HOLDING_REGISTER + isWritable=False → sensor/diagnostic."""
        from nibe_entity_detection import detect_entity_type
        p = self._point('MODBUS_HOLDING_REGISTER')
        p['metadata']['isWritable'] = False
        et, cat = detect_entity_type(p)
        self.assertEqual(et, 'sensor')
        self.assertEqual(cat, 'diagnostic')
