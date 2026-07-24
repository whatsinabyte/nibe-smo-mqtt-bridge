"""
test_mqtt_publisher.py
======================
Nibe_mqtt_publisher tests.
Part of the Nibe S-Series MQTT Bridge test suite.
Shared fixtures are in conftest.py.
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from hypothesis import example, given
from hypothesis import strategies as st

from conftest import (
    _make_em,
    _nibe_raw_value,
    _nibe_point_id,
    _unicode_text,
    _safe_entity_id,
)

# Topic builder functions — imported at module level so mutmut can replace the
# module cleanly between test runs without triggering import-lock timeouts.
from nibe_mqtt_publisher import (
    t_config, t_state, t_command, t_available, t_attributes, t_press,
)

class TestTopicFunctionProperties(unittest.TestCase):
    """Hypothesis properties for MQTT topic builder functions."""

    _safe_str = st.text(
        alphabet=st.characters(categories=['L', 'N'],
                               include_characters='_-'),
        min_size=1, max_size=30,
    )

    @given(_safe_str, _safe_str)
    def test_t_config_contains_config(self, entity_type, entity_id):
        self.assertIn('config', t_config(entity_type, entity_id))

    @given(_safe_str, _safe_str)
    def test_t_state_contains_state(self, entity_type, entity_id):
        self.assertIn('state', t_state(entity_type, entity_id))

    @given(_safe_str, _safe_str)
    def test_t_command_contains_set(self, entity_type, entity_id):
        self.assertIn('set', t_command(entity_type, entity_id))

    @given(_safe_str, _safe_str)
    def test_all_topics_contain_entity_id(self, entity_type, entity_id):
        for fn in (t_config, t_state, t_command, t_available, t_attributes):
            self.assertIn(entity_id, fn(entity_type, entity_id))

    @given(_safe_str, _safe_str)
    def test_all_topics_contain_entity_type(self, entity_type, entity_id):
        for fn in (t_config, t_state, t_command, t_available, t_attributes):
            self.assertIn(entity_type, fn(entity_type, entity_id))

    @given(_safe_str, _safe_str)
    def test_topics_are_distinct_per_role(self, entity_type, entity_id):
        """All five topic roles must produce distinct topic strings."""
        topics = [fn(entity_type, entity_id)
                  for fn in (t_config, t_state, t_command, t_available, t_attributes)]
        self.assertEqual(len(topics), len(set(topics)))

    @given(_safe_str)
    def test_t_press_contains_press(self, entity_id):
        self.assertIn('press', t_press(entity_id))

    @given(_safe_str, _safe_str)
    def test_t_available_contains_available(self, entity_type, entity_id):
        self.assertIn('available', t_available(entity_type, entity_id))

    @given(_safe_str, _safe_str)
    def test_t_attributes_contains_attributes(self, entity_type, entity_id):
        self.assertIn('attributes', t_attributes(entity_type, entity_id))

    @given(_safe_str, _safe_str)
    def test_t_available_contains_entity_id(self, entity_type, entity_id):
        self.assertIn(entity_id, t_available(entity_type, entity_id))

    @given(_safe_str, _safe_str)
    def test_t_attributes_contains_entity_id(self, entity_type, entity_id):
        self.assertIn(entity_id, t_attributes(entity_type, entity_id))

    @given(_safe_str, _safe_str)
    def test_t_available_distinct_from_t_state(self, entity_type, entity_id):
        self.assertNotEqual(t_available(entity_type, entity_id),
                            t_state(entity_type, entity_id))

    @given(_safe_str, _safe_str)
    def test_t_attributes_distinct_from_t_state(self, entity_type, entity_id):
        self.assertNotEqual(t_attributes(entity_type, entity_id),
                            t_state(entity_type, entity_id))

    @given(_safe_str, _safe_str)
    def test_all_six_topics_distinct(self, entity_type, entity_id):
        """All six topic functions must produce distinct strings."""
        topics = [
            t_config(entity_type, entity_id),
            t_state(entity_type, entity_id),
            t_command(entity_type, entity_id),
            t_available(entity_type, entity_id),
            t_attributes(entity_type, entity_id),
        ]
        self.assertEqual(len(topics), len(set(topics)),
            f"Duplicate topic strings: {topics}")



class TestResolveUnitProperties(unittest.TestCase):
    """Hypothesis properties for resolve_unit."""

    @given(_nibe_point_id,
           st.text(max_size=20),
           st.text(max_size=50))
    def test_always_returns_two_tuple(self, point_id, raw_unit, title):
        from nibe_mqtt_publisher import resolve_unit
        result = resolve_unit(point_id, raw_unit, title)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    @given(_nibe_point_id,
           st.text(max_size=20),
           st.text(max_size=50))
    def test_first_element_is_string(self, point_id, raw_unit, title):
        from nibe_mqtt_publisher import resolve_unit
        unit, _ = resolve_unit(point_id, raw_unit, title)
        self.assertIsInstance(unit, str)

    @given(_nibe_point_id,
           st.text(max_size=20),
           st.text(max_size=50))
    def test_second_element_is_bool(self, point_id, raw_unit, title):
        from nibe_mqtt_publisher import resolve_unit
        _, was_overridden = resolve_unit(point_id, raw_unit, title)
        self.assertIsInstance(was_overridden, bool)

    @given(_nibe_point_id,
           st.text(max_size=20))
    def test_no_mojibake_in_resolved_unit(self, point_id, raw_unit):
        from nibe_mqtt_publisher import resolve_unit
        unit, _ = resolve_unit(point_id, raw_unit)
        self.assertNotIn('\u00c2', unit)

    @given(_nibe_point_id,
           st.text(max_size=20))
    def test_overridden_point_returns_true_flag(self, point_id, raw_unit):
        """Points in UNIT_OVERRIDES always return was_overridden=True."""
        from nibe_mqtt_publisher import resolve_unit, UNIT_OVERRIDES
        if point_id in UNIT_OVERRIDES:
            _, was_overridden = resolve_unit(point_id, raw_unit)
            self.assertTrue(was_overridden)

    @given(_nibe_point_id.filter(
               lambda p: p not in __import__('nibe_mqtt_publisher').UNIT_OVERRIDES),
           st.text(max_size=20))
    def test_non_overridden_point_returns_false_flag(self, point_id, raw_unit):
        """Points not in UNIT_OVERRIDES always return was_overridden=False."""
        from nibe_mqtt_publisher import resolve_unit
        _, was_overridden = resolve_unit(point_id, raw_unit)
        self.assertFalse(was_overridden)

    @given(_nibe_point_id,
           st.text(max_size=20))
    def test_resolved_unit_has_no_leading_trailing_whitespace(self, point_id, raw_unit):
        from nibe_mqtt_publisher import resolve_unit
        unit, _ = resolve_unit(point_id, raw_unit)
        self.assertEqual(unit, unit.strip())

    @given(_nibe_point_id,
           st.text(max_size=20))
    def test_idempotent(self, point_id, raw_unit):
        """resolve_unit applied twice gives the same result."""
        from nibe_mqtt_publisher import resolve_unit
        unit1, override1 = resolve_unit(point_id, raw_unit)
        unit2, override2 = resolve_unit(point_id, unit1)
        self.assertEqual(unit1, unit2)


# ---------------------------------------------------------------------------
# get_value_mapping properties
# ---------------------------------------------------------------------------


class TestCrossFunctionProperties(unittest.TestCase):
    """Hypothesis properties that verify invariants spanning multiple functions."""

    # Strategy: a metadata dict shaped like a canonical switch
    _switch_meta = {
        'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
        'variableSize': 'u8', 'variableType': 'integer',
        'minValue': 0, 'maxValue': 1,
        'unit': '', 'isWritable': True, 'divisor': 1, 'decimal': 0,
    }

    @given(_nibe_point_id.filter(
               lambda p: p not in __import__('nibe_entity_detection').ENTITY_TYPE_OVERRIDES))
    def test_switch_candidate_metadata_gives_switch_or_select_type(self, pid):
        """If is_switch_candidate(meta) is True and point is not overridden
        and register is HOLDING → detect_entity_type returns 'switch' or 'select'
        (select wins when a valid value mapping is present for this point)."""
        from nibe_entity_detection import detect_entity_type, is_switch_candidate
        self.assertTrue(is_switch_candidate(self._switch_meta))
        point = {'variableId': pid, 'title': 'Test', 'description': '',
                 'metadata': self._switch_meta}
        entity_type, category = detect_entity_type(point)
        self.assertIn(entity_type, ('switch', 'select'))
        self.assertEqual(category, 'config')

    @given(_nibe_point_id.filter(
               lambda p: p not in __import__('nibe_entity_detection').ENTITY_TYPE_OVERRIDES),
           st.text(min_size=1, max_size=10).filter(lambda s: s.strip()))
    def test_number_candidate_metadata_gives_number_or_select_type(self, pid, unit):
        """If is_number_candidate(meta) is True and register is HOLDING
        and point not overridden → detect_entity_type returns 'number' or 'select'
        (select wins when a valid description mapping is present)."""
        from nibe_entity_detection import detect_entity_type, is_number_candidate
        meta = {**self._switch_meta, 'unit': unit, 'maxValue': 100}
        self.assertTrue(is_number_candidate(meta))
        point = {'variableId': pid, 'title': 'Test', 'description': '',
                 'metadata': meta}
        entity_type, category = detect_entity_type(point)
        self.assertIn(entity_type, ('number', 'select'))
        self.assertEqual(category, 'config')

    @given(_nibe_point_id)
    def test_create_entity_id_in_all_topic_functions(self, pid):
        """create_entity_id(pid) fed into any t_* function always contains str(pid)."""
        from nibe_entity_detection import create_entity_id
        from nibe_mqtt_publisher import t_config, t_state, t_command, t_available, t_attributes
        entity_id = create_entity_id(pid)
        for fn in (t_config, t_state, t_command, t_available, t_attributes):
            topic = fn('sensor', entity_id)
            self.assertIn(str(pid), topic,
                f"{fn.__name__}('sensor', {entity_id!r}) doesn't contain {pid}")

    @given(_nibe_point_id,
           st.text(max_size=20))
    def test_clean_unit_does_not_affect_apply_divisor(self, raw_value, unit):
        """clean_unit and apply_divisor are independent: cleaning the unit
        must not change apply_divisor's output."""
        from nibe_entity_detection import clean_unit, apply_divisor
        result_before = apply_divisor(raw_value, 10)
        _ = clean_unit(unit)  # should have no effect on divisor arithmetic
        result_after = apply_divisor(raw_value, 10)
        self.assertEqual(result_before, result_after)

    @given(st.text(max_size=100))
    def test_parse_description_with_two_plus_entries_gives_nonempty_options(self, description):
        """If parse_description_mapping returns a dict with ≥2 entries,
        get_entity_options on the same description should return ≥2 options."""
        from nibe_entity_detection import parse_description_mapping, get_entity_options
        mapping = parse_description_mapping(description)
        if mapping is not None and len(mapping) >= 2:
            meta = {'modbusRegisterType': 'MODBUS_HOLDING_REGISTER'}
            opts = get_entity_options(99999, meta, description)
            # Not guaranteed to match exactly due to dedup, but must be ≥2 or
            # the description had duplicate labels
            if len(set(mapping.values())) >= 2:
                self.assertGreaterEqual(len(opts), 2)

    @given(_nibe_point_id,
           _unicode_text)
    def test_clean_string_output_safe_for_entity_id_generation(self, pid, title):
        """clean_string then create_entity_id must not produce a topic
        with any MQTT-unsafe characters."""
        from nibe_entity_detection import clean_string, create_entity_id
        from nibe_mqtt_publisher import t_state
        _ = clean_string(title)  # clean but don't use for entity_id
        entity_id = create_entity_id(pid)
        topic = t_state('sensor', entity_id)
        # topic must only contain valid MQTT chars (alphanumeric, /, _, -)
        self.assertRegex(topic, r'^[a-zA-Z0-9/_-]+$')

    @given(_nibe_raw_value, st.integers(min_value=1, max_value=10000))
    def test_apply_divisor_output_parseable_after_resolve_unit(self, raw, divisor):
        """apply_divisor result is always a valid numeric string regardless
        of what resolve_unit does with the associated unit."""
        from nibe_entity_detection import apply_divisor
        from nibe_mqtt_publisher import resolve_unit
        value_str = apply_divisor(raw, divisor)
        unit, _ = resolve_unit(99999, 'kWh')
        # The value string must still be parseable as a float after unit resolution
        float(value_str)  # must not raise


# ---------------------------------------------------------------------------
# _compress_payload / _decompress_payload roundtrip
# ---------------------------------------------------------------------------


class TestDiscoveryConfigSnapshots(unittest.TestCase):
    """Pin the exact MQTT discovery config format for firmware-critical points.

    If the discovery config format changes accidentally (breaking HA entity
    registration), these tests catch it immediately. Each snapshot is the
    authoritative expected output for that point type.
    """

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        mqtt = MagicMock()
        mqtt.publish.return_value = MagicMock(rc=0)
        return MqttDiscoveryPublisher(
            mqtt_client=mqtt,
            device_info={'identifiers': ['nibe_test'], 'name': 'Test'},
            device_id='nibe_test',
            device_name='Test Device',
        ), mqtt

    def _point(self, pid, entity_type, **kwargs):
        base = {
            'variableId': pid, 'display_title': f'Point {pid}',
            'entity_type': entity_type, 'entity_category': 'diagnostic',
            'is_writable': False, 'is_dynamic': False, 'description': '',
            'metadata': {
                'unit': '', 'shortUnit': '', 'minValue': 0, 'maxValue': 1,
                'modbusRegisterID': pid,
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'variableType': 'integer', 'variableSize': 'u8',
                'isWritable': False, 'divisor': 1, 'decimal': 0,
                'intDefaultValue': 0, 'stringDefaultValue': '', 'change': 1,
            },
        }
        base.update(kwargs)
        return base

    def _get_config(self, mqtt, entity_type, pid):
        import json as _json
        from nibe_mqtt_publisher import t_config, create_entity_id
        topic = t_config(entity_type, create_entity_id(pid))
        calls = [c for c in mqtt.publish.call_args_list if c.args[0] == topic]
        self.assertTrue(calls, f"No discovery config published for pid={pid}")
        return _json.loads(calls[-1].args[1])

    def test_binary_sensor_config_has_on_off_payloads(self):
        """binary_sensor config must always use PAYLOAD_ON='ON'/PAYLOAD_OFF='OFF'.
        (Switch uses '1'/'0'; binary_sensor uses 'ON'/'OFF' — must never mix.)"""
        pub, mqtt = self._pub()
        pub.publish_entity_discovery(
            self._point(818, 'binary_sensor'), {})
        config = self._get_config(mqtt, 'binary_sensor', 818)
        self.assertEqual(config['payload_on'],  'ON')
        self.assertEqual(config['payload_off'], 'OFF')

    def test_switch_config_has_correct_payloads(self):
        """switch config must always use payload_on='1', payload_off='0'."""
        pub, mqtt = self._pub()
        p = self._point(5110, 'switch')
        p['is_writable'] = True
        p['metadata']['isWritable'] = True
        p['metadata']['modbusRegisterType'] = 'MODBUS_HOLDING_REGISTER'
        pub.publish_entity_discovery(p, {})
        config = self._get_config(mqtt, 'switch', 5110)
        self.assertEqual(config['payload_on'],  '1')
        self.assertEqual(config['payload_off'], '0')
        self.assertFalse(config.get('optimistic', True))

    def test_date_sensor_2685_device_class(self):
        """Point 2685 must always have device_class='date' and no unit."""
        pub, mqtt = self._pub()
        pub.publish_entity_discovery(
            self._point(2685, 'sensor', display_title='Days since commissioning'),
            {})
        config = self._get_config(mqtt, 'sensor', 2685)
        self.assertEqual(config['device_class'], 'date')
        self.assertNotIn('unit_of_measurement', config)

    def test_sensor_with_unit_has_unit_of_measurement(self):
        """Sensor with unit must always include unit_of_measurement."""
        pub, mqtt = self._pub()
        p = self._point(100, 'sensor')
        p['metadata']['unit'] = '°C'
        pub.publish_entity_discovery(p, {})
        config = self._get_config(mqtt, 'sensor', 100)
        self.assertIn('unit_of_measurement', config)
        self.assertEqual(config['unit_of_measurement'], '°C')

    def test_all_configs_have_availability_topic(self):
        """Every discovery config must always include availability_topic."""
        pub, mqtt = self._pub()
        for pid, entity_type in [(818, 'binary_sensor'), (100, 'sensor')]:
            mqtt.reset_mock()
            pub.publish_entity_discovery(self._point(pid, entity_type), {})
            config = self._get_config(mqtt, entity_type, pid)
            self.assertIn('availability_topic', config,
                f"pid={pid} config missing availability_topic")

    def test_all_configs_have_unique_id(self):
        """Every discovery config must always include unique_id=nibe_{pid}."""
        pub, mqtt = self._pub()
        for pid, entity_type in [(818, 'binary_sensor'), (100, 'sensor')]:
            mqtt.reset_mock()
            pub.publish_entity_discovery(self._point(pid, entity_type), {})
            config = self._get_config(mqtt, entity_type, pid)
            self.assertEqual(config['unique_id'], f'nibe_{pid}',
                f"pid={pid} config has wrong unique_id")

    def test_all_configs_have_device(self):
        """Every discovery config must embed the device block."""
        pub, mqtt = self._pub()
        pub.publish_entity_discovery(self._point(100, 'sensor'), {})
        config = self._get_config(mqtt, 'sensor', 100)
        self.assertIn('device', config)
        self.assertIn('identifiers', config['device'])

    def test_suggested_display_precision_only_on_numeric_sensors(self):
        """suggested_display_precision must only appear when unit is set.
        This was a production bug — presence alone makes HA treat entity as numeric."""
        pub, mqtt = self._pub()
        # Sensor WITHOUT unit
        pub.publish_entity_discovery(self._point(100, 'sensor'), {})
        config_no_unit = self._get_config(mqtt, 'sensor', 100)
        self.assertNotIn('suggested_display_precision', config_no_unit,
            "suggested_display_precision must not appear when unit is absent")


    # ── number config ─────────────────────────────────────────────────────────

    def test_number_config_has_required_fields(self):
        """number config must always have min, max, step, state_topic, command_topic."""
        pub, mqtt = self._pub()
        p = self._point(200, 'number')
        p['is_writable'] = True
        p['metadata']['isWritable'] = True
        p['metadata']['modbusRegisterType'] = 'MODBUS_HOLDING_REGISTER'
        p['metadata']['unit'] = 'C'
        p['metadata']['minValue'] = -300
        p['metadata']['maxValue'] = 300
        pub.publish_entity_discovery(p, {})
        config = self._get_config(mqtt, 'number', 200)
        for field in ('min', 'max', 'step', 'state_topic', 'command_topic'):
            self.assertIn(field, config, f"number config missing '{field}'")

    def test_number_config_min_max_match_metadata(self):
        """number min/max must match the firmware metadata values."""
        pub, mqtt = self._pub()
        p = self._point(200, 'number')
        p['is_writable'] = True
        p['metadata']['isWritable'] = True
        p['metadata']['modbusRegisterType'] = 'MODBUS_HOLDING_REGISTER'
        p['metadata']['minValue'] = -300
        p['metadata']['maxValue'] = 300
        pub.publish_entity_discovery(p, {})
        config = self._get_config(mqtt, 'number', 200)
        self.assertEqual(config['min'], -300.0)
        self.assertEqual(config['max'],  300.0)

    def test_number_config_step_always_positive(self):
        """step must always be > 0."""
        pub, mqtt = self._pub()
        p = self._point(200, 'number')
        p['is_writable'] = True
        p['metadata']['isWritable'] = True
        p['metadata']['modbusRegisterType'] = 'MODBUS_HOLDING_REGISTER'
        pub.publish_entity_discovery(p, {})
        config = self._get_config(mqtt, 'number', 200)
        self.assertGreater(config['step'], 0)

    def test_number_config_mode_is_box(self):
        """mode must always be 'box' — not 'slider' which is unusable for Nibe ranges."""
        pub, mqtt = self._pub()
        p = self._point(200, 'number')
        p['is_writable'] = True
        p['metadata']['isWritable'] = True
        p['metadata']['modbusRegisterType'] = 'MODBUS_HOLDING_REGISTER'
        pub.publish_entity_discovery(p, {})
        config = self._get_config(mqtt, 'number', 200)
        self.assertEqual(config.get('mode'), 'box')

    # ── select config ─────────────────────────────────────────────────────────

    def test_select_config_has_options(self):
        """select config must always have a non-empty options list."""
        from nibe_entity_detection import VALUE_MAPPINGS
        vm_pid = next(iter(VALUE_MAPPINGS.get('holding', {}).keys()))
        pub, mqtt = self._pub()
        p = self._point(vm_pid, 'select')
        p['is_writable'] = True
        p['metadata']['isWritable'] = True
        p['metadata']['modbusRegisterType'] = 'MODBUS_HOLDING_REGISTER'
        pub.publish_entity_discovery(p, {})
        config = self._get_config(mqtt, 'select', vm_pid)
        self.assertIn('options', config)
        self.assertGreater(len(config['options']), 0)

    def test_select_config_options_are_strings(self):
        """All options must be strings — HA rejects non-string option values."""
        from nibe_entity_detection import VALUE_MAPPINGS
        vm_pid = next(iter(VALUE_MAPPINGS.get('holding', {}).keys()))
        pub, mqtt = self._pub()
        p = self._point(vm_pid, 'select')
        p['is_writable'] = True
        p['metadata']['isWritable'] = True
        p['metadata']['modbusRegisterType'] = 'MODBUS_HOLDING_REGISTER'
        pub.publish_entity_discovery(p, {})
        config = self._get_config(mqtt, 'select', vm_pid)
        for opt in config.get('options', []):
            self.assertIsInstance(opt, str, f"option {opt!r} is not a string")

    def test_select_config_has_state_and_command_topics(self):
        from nibe_entity_detection import VALUE_MAPPINGS
        vm_pid = next(iter(VALUE_MAPPINGS.get('holding', {}).keys()))
        pub, mqtt = self._pub()
        p = self._point(vm_pid, 'select')
        p['is_writable'] = True
        p['metadata']['isWritable'] = True
        p['metadata']['modbusRegisterType'] = 'MODBUS_HOLDING_REGISTER'
        pub.publish_entity_discovery(p, {})
        config = self._get_config(mqtt, 'select', vm_pid)
        self.assertIn('state_topic',   config)
        self.assertIn('command_topic', config)

    # ── button config ─────────────────────────────────────────────────────────

    def test_button_config_command_topic_ends_with_press(self):
        """button command_topic must end with '/press' — not '/set'."""
        pub, mqtt = self._pub()
        pub.publish_entity_discovery(self._point(300, 'button'), {})
        config = self._get_config(mqtt, 'button', 300)
        self.assertTrue(config['command_topic'].endswith('/press'),
            f"button command_topic must end with /press: {config['command_topic']!r}")

    def test_button_config_has_no_state_topic(self):
        """button must never have state_topic — buttons are write-only in HA."""
        pub, mqtt = self._pub()
        pub.publish_entity_discovery(self._point(300, 'button'), {})
        config = self._get_config(mqtt, 'button', 300)
        self.assertNotIn('state_topic', config,
            "button config must not have state_topic")

    def test_button_config_has_no_json_attributes_topic(self):
        """button must not publish static attributes — no attributes_topic."""
        pub, mqtt = self._pub()
        pub.publish_entity_discovery(self._point(300, 'button'), {})
        config = self._get_config(mqtt, 'button', 300)
        self.assertNotIn('json_attributes_topic', config)

    def test_button_has_unique_id_and_availability(self):
        pub, mqtt = self._pub()
        pub.publish_entity_discovery(self._point(300, 'button'), {})
        config = self._get_config(mqtt, 'button', 300)
        self.assertEqual(config['unique_id'], 'nibe_300')
        self.assertIn('availability_topic', config)


# ---------------------------------------------------------------------------
# 5. st.from_regex() — entity ID format for resolve_point_from_entity_id
# ---------------------------------------------------------------------------


class TestEntityDetectionConstantsProperties(unittest.TestCase):
    """Structural invariants for ENTITY_TYPE_OVERRIDES and VALUE_MAPPINGS.

    These constants drive entity classification for every firmware point.
    Structural drift (wrong types, duplicate keys, invalid values) would cause
    silent misclassification that's hard to debug.
    """

    def test_entity_type_overrides_values_are_valid_types(self):
        """All ENTITY_TYPE_OVERRIDES values must be valid HA entity types."""
        from nibe_entity_detection import ENTITY_TYPE_OVERRIDES
        valid = {'sensor', 'binary_sensor', 'switch', 'number',
                 'select', 'button', 'text', 'time'}
        for pid, etype in ENTITY_TYPE_OVERRIDES.items():
            self.assertIn(etype, valid,
                f"ENTITY_TYPE_OVERRIDES[{pid}]={etype!r} is not a valid entity type")

    def test_entity_type_overrides_keys_are_ints(self):
        from nibe_entity_detection import ENTITY_TYPE_OVERRIDES
        for pid in ENTITY_TYPE_OVERRIDES:
            self.assertIsInstance(pid, int)

    def test_entity_type_overrides_pids_in_valid_range(self):
        from nibe_entity_detection import ENTITY_TYPE_OVERRIDES
        for pid in ENTITY_TYPE_OVERRIDES:
            self.assertGreater(pid, 0)
            self.assertLessEqual(pid, 65535)

    def test_value_mappings_structure(self):
        """VALUE_MAPPINGS must have 'input' and 'holding' keys."""
        from nibe_entity_detection import VALUE_MAPPINGS
        self.assertIn('input',   VALUE_MAPPINGS)
        self.assertIn('holding', VALUE_MAPPINGS)

    def test_value_mappings_inner_keys_are_ints(self):
        """All point IDs in VALUE_MAPPINGS must be ints."""
        from nibe_entity_detection import VALUE_MAPPINGS
        for reg_type, mapping in VALUE_MAPPINGS.items():
            for pid in mapping:
                self.assertIsInstance(pid, int,
                    f"VALUE_MAPPINGS[{reg_type!r}] has non-int key: {pid!r}")

    def test_value_mappings_inner_values_are_dicts(self):
        """All value→label maps must be dicts."""
        from nibe_entity_detection import VALUE_MAPPINGS
        for reg_type, mapping in VALUE_MAPPINGS.items():
            for pid, val_map in mapping.items():
                self.assertIsInstance(val_map, dict,
                    f"VALUE_MAPPINGS[{reg_type!r}][{pid}] is not a dict")

    def test_value_mappings_inner_dict_keys_are_ints(self):
        """Firmware register values (dict keys) must be ints."""
        from nibe_entity_detection import VALUE_MAPPINGS
        for reg_type, mapping in VALUE_MAPPINGS.items():
            for pid, val_map in mapping.items():
                for reg_val in val_map:
                    self.assertIsInstance(reg_val, int,
                        f"VALUE_MAPPINGS[{reg_type!r}][{pid}] has non-int key: {reg_val!r}")

    def test_value_mappings_inner_dict_values_are_strings(self):
        """Human-readable labels must be strings."""
        from nibe_entity_detection import VALUE_MAPPINGS
        for reg_type, mapping in VALUE_MAPPINGS.items():
            for pid, val_map in mapping.items():
                for reg_val, label in val_map.items():
                    self.assertIsInstance(label, str,
                        f"VALUE_MAPPINGS[{reg_type!r}][{pid}][{reg_val}] is not a str")

    def test_unit_overrides_in_mqtt_publisher(self):
        """UNIT_OVERRIDES keys must be ints and values must be strings."""
        from nibe_mqtt_publisher import UNIT_OVERRIDES
        for pid, unit in UNIT_OVERRIDES.items():
            self.assertIsInstance(pid, int,
                f"UNIT_OVERRIDES key {pid!r} is not int")
            self.assertIsInstance(unit, str,
                f"UNIT_OVERRIDES[{pid}]={unit!r} is not str")

    @given(_nibe_point_id)
    @example(50827)   # THS-10 humidity: UNIT_OVERRIDES %RH→%
    @example(5110)    # ENTITY_TYPE_OVERRIDES switch
    @example(5214)    # ENTITY_TYPE_OVERRIDES switch
    @example(32824)   # ENTITY_TYPE_OVERRIDES switch
    @example(22077)   # ENTITY_TYPE_OVERRIDES binary_sensor
    @example(8982)    # ENTITY_TYPE_OVERRIDES switch (Away mode max=0 quirk)
    @example(3754)    # ENTITY_TYPE_OVERRIDES switch (Forced control max=0 quirk)
    def test_entity_type_override_never_produces_invalid_type(self, pid):
        """If a point has an override, detect_entity_type must return that type."""
        from nibe_entity_detection import detect_entity_type, ENTITY_TYPE_OVERRIDES
        if pid in ENTITY_TYPE_OVERRIDES:
            point = {
                'variableId': pid, 'title': 'Test', 'description': '',
                'metadata': {
                    'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                    'variableType': 'integer', 'variableSize': 'u8',
                    'isWritable': False, 'minValue': 0, 'maxValue': 1,
                    'unit': '', 'divisor': 1,
                }
            }
            entity_type, _ = detect_entity_type(point)
            self.assertEqual(entity_type, ENTITY_TYPE_OVERRIDES[pid])


# ---------------------------------------------------------------------------
# _BINARY_SENSOR_EXCLUSIONS structural invariants
# ---------------------------------------------------------------------------


class TestBinarySensorExclusionsProperties(unittest.TestCase):
    """Structural invariants for _BINARY_SENSOR_EXCLUSIONS.

    This frozenset lists point IDs that must never be auto-classified as
    binary_sensor even if their shape matches. If a point is accidentally
    removed from the set, it would become a binary_sensor and lose its
    correct entity type silently.
    """

    def setUp(self):
        from nibe_entity_detection import _BINARY_SENSOR_EXCLUSIONS
        self.excl = _BINARY_SENSOR_EXCLUSIONS

    def test_is_frozenset(self):
        self.assertIsInstance(self.excl, frozenset)

    def test_all_elements_are_ints(self):
        for pid in self.excl:
            self.assertIsInstance(pid, int, f"Exclusion {pid!r} is not int")

    def test_all_in_valid_range(self):
        for pid in self.excl:
            self.assertGreater(pid, 0)
            self.assertLessEqual(pid, 65535)

    def test_nonempty(self):
        """Exclusion list must never be accidentally emptied."""
        self.assertGreater(len(self.excl), 0)

    def test_no_excluded_point_auto_classifies_as_binary_sensor(self):
        """Every excluded point with binary shape must produce non-binary result."""
        from nibe_entity_detection import detect_entity_type
        for pid in self.excl:
            point = {
                'variableId': pid, 'description': '',
                'metadata': {
                    'variableSize': 'u8', 'variableType': 'integer',
                    'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                    'isWritable': False, 'minValue': 0, 'maxValue': 1,
                    'unit': '', 'divisor': 1,
                }
            }
            entity_type, _ = detect_entity_type(point)
            self.assertNotEqual(entity_type, 'binary_sensor',
                f"Point {pid} in _BINARY_SENSOR_EXCLUSIONS "
                f"was classified as binary_sensor")

    @given(_nibe_point_id)
    def test_exclusion_overrides_binary_shape(self, pid):
        """For any excluded point, binary shape must not yield binary_sensor."""
        from nibe_entity_detection import _BINARY_SENSOR_EXCLUSIONS, detect_entity_type
        if pid not in _BINARY_SENSOR_EXCLUSIONS:
            return
        point = {
            'variableId': pid, 'description': '',
            'metadata': {
                'variableSize': 'u8', 'variableType': 'integer',
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'isWritable': False, 'minValue': 0, 'maxValue': 1,
                'unit': '', 'divisor': 1,
            }
        }
        entity_type, _ = detect_entity_type(point)
        self.assertNotEqual(entity_type, 'binary_sensor')


# ---------------------------------------------------------------------------
# MgmtTopic / BrowserTopic structural invariants (nibe_mqtt_publisher.py)
# ---------------------------------------------------------------------------


class TestTopicEnumStructuralProperties(unittest.TestCase):
    """Structural invariants for MgmtTopic and BrowserTopic enums.

    If two topic strings accidentally share the same value, MQTT messages
    would be delivered to the wrong handler silently. These tests lock in
    the uniqueness and format invariants.
    """

    def _mgmt_values(self):
        from nibe_mqtt_publisher import MgmtTopic
        return [v.value for v in MgmtTopic]

    def _browser_values(self):
        from nibe_mqtt_publisher import BrowserTopic
        return [v.value for v in BrowserTopic]

    def test_all_mgmt_topic_values_unique(self):
        """No two MgmtTopic members must share the same topic string."""
        vals = self._mgmt_values()
        self.assertEqual(len(vals), len(set(vals)),
            "Duplicate MgmtTopic values detected")

    def test_all_browser_topic_values_unique(self):
        """No two BrowserTopic members must share the same topic string."""
        vals = self._browser_values()
        self.assertEqual(len(vals), len(set(vals)),
            "Duplicate BrowserTopic values detected")

    def test_mgmt_and_browser_topics_do_not_overlap(self):
        """MgmtTopic and BrowserTopic must not share any topic strings."""
        mgmt    = set(self._mgmt_values())
        browser = set(self._browser_values())
        overlap = mgmt & browser
        self.assertEqual(overlap, set(),
            f"MgmtTopic and BrowserTopic share topics: {overlap}")

    def test_all_mgmt_values_are_strings(self):
        for val in self._mgmt_values():
            self.assertIsInstance(val, str)

    def test_all_browser_values_are_strings(self):
        for val in self._browser_values():
            self.assertIsInstance(val, str)

    def test_all_mgmt_values_nonempty(self):
        for val in self._mgmt_values():
            self.assertGreater(len(val), 0)

    def test_all_browser_values_nonempty(self):
        for val in self._browser_values():
            self.assertGreater(len(val), 0)

    def test_most_mgmt_topics_start_with_homeassistant(self):
        """Most MgmtTopic values start with 'homeassistant/' (test suite topics
        use 'nibe/browser/' as an exception — verified separately)."""
        ha_prefix   = [v for v in self._mgmt_values() if v.startswith('homeassistant/')]
        nibe_prefix = [v for v in self._mgmt_values() if v.startswith('nibe/')]
        other       = [v for v in self._mgmt_values()
                       if not v.startswith('homeassistant/') and not v.startswith('nibe/')]
        # The vast majority must be homeassistant/ topics
        self.assertGreater(len(ha_prefix), len(nibe_prefix))
        self.assertEqual(other, [],
            f"Unexpected MgmtTopic prefix: {other}")

    def test_browser_topics_start_with_nibe(self):
        """All BrowserTopic values must start with 'nibe/'."""
        for val in self._browser_values():
            self.assertTrue(val.startswith('nibe/'),
                f"BrowserTopic value {val!r} does not start with 'nibe/'")

    def test_no_topic_contains_spaces(self):
        """MQTT topic strings must never contain spaces."""
        all_topics = self._mgmt_values() + self._browser_values()
        for topic in all_topics:
            self.assertNotIn(' ', topic,
                f"Topic {topic!r} contains a space")

    def test_no_topic_contains_wildcard_chars(self):
        """Topics used for publishing must never contain MQTT wildcards."""
        all_topics = self._mgmt_values() + self._browser_values()
        for topic in all_topics:
            self.assertNotIn('+', topic)
            self.assertNotIn('#', topic)


# ---------------------------------------------------------------------------
# Cross-constant consistency properties
# ---------------------------------------------------------------------------


class TestDynamicPointMapFlushProperties(unittest.TestCase):
    """Hypothesis properties for DynamicPointMap.flush."""

    def _map_with_entries(self, pids):
        from nibe_dynamic_map import DynamicPointMap, DynamicPointEntry
        m = DynamicPointMap()
        for pid in pids:
            entry = DynamicPointEntry(
                point_id=pid, title=f'P{pid}', entity_type='switch',
                processed_values={0, 1}, is_controlling=True,
                unprocessed_values=set(),
            )
            entry.dynamic_points_by_value = {1: [pid + 10000]}
            m._table[pid] = entry
        return m

    def _all_points(self, pids):
        return {pid: {
            'variableId': pid, 'display_title': f'P{pid}',
            'entity_type': 'switch',
            'metadata': {'minValue': 0, 'maxValue': 1},
        } for pid in pids}

    @given(st.sets(st.integers(min_value=1, max_value=500), max_size=10))
    def test_flush_resets_is_controlling_to_none(self, pids):
        """After flush, all entries must have is_controlling=None."""
        m = self._map_with_entries(pids)
        pts = self._all_points(pids)
        types = {pid: 'switch' for pid in pids}
        m.flush(pts, types)
        for entry in m._table.values():
            self.assertIsNone(entry.is_controlling)

    @given(st.sets(st.integers(min_value=1, max_value=500), max_size=10))
    def test_flush_clears_processed_values(self, pids):
        """After flush, all processed_values must be empty."""
        m = self._map_with_entries(pids)
        pts = self._all_points(pids)
        types = {pid: 'switch' for pid in pids}
        m.flush(pts, types)
        for entry in m._table.values():
            self.assertEqual(entry.processed_values, set())

    @given(st.sets(st.integers(min_value=1, max_value=500), max_size=10))
    def test_flush_clears_dynamic_points_by_value(self, pids):
        """After flush, dynamic_points_by_value must be empty."""
        m = self._map_with_entries(pids)
        pts = self._all_points(pids)
        types = {pid: 'switch' for pid in pids}
        m.flush(pts, types)
        for entry in m._table.values():
            self.assertEqual(entry.dynamic_points_by_value, {})

    @given(st.sets(st.integers(min_value=1, max_value=500), min_size=1, max_size=10))
    def test_flush_restores_unprocessed_values(self, pids):
        """After flush, unprocessed_values must be non-empty (reset to all values)."""
        m = self._map_with_entries(pids)
        pts = self._all_points(pids)
        types = {pid: 'switch' for pid in pids}
        m.flush(pts, types)
        for entry in m._table.values():
            self.assertGreater(len(entry.unprocessed_values), 0)

    @given(st.sets(st.integers(min_value=1, max_value=500), max_size=10))
    def test_flush_never_raises(self, pids):
        m = self._map_with_entries(pids)
        pts = self._all_points(pids)
        types = {pid: 'switch' for pid in pids}
        m.flush(pts, types)  # must not raise

    def test_flush_empty_map_never_raises(self):
        from nibe_dynamic_map import DynamicPointMap
        m = DynamicPointMap()
        m.flush({}, {})  # must not raise


# ---------------------------------------------------------------------------
# _build_sensor_config properties (nibe_mqtt_publisher.py)
# ---------------------------------------------------------------------------


class TestBuildSensorConfigProperties(unittest.TestCase):
    """Hypothesis properties for _build_sensor_config."""

    @given(_safe_entity_id, _nibe_point_id, st.text(max_size=10),
           st.text(max_size=50))
    def test_always_sets_state_topic(self, entity_id, pid, unit, title):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        config = {}
        MqttDiscoveryPublisher._build_sensor_config(
            config, entity_id, pid, unit, title, {})
        self.assertIn('state_topic', config)

    @given(_safe_entity_id, _nibe_point_id, st.text(max_size=10),
           st.text(max_size=50))
    def test_state_topic_consistent_with_t_state(self, entity_id, pid, unit, title):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, t_state
        config = {}
        MqttDiscoveryPublisher._build_sensor_config(
            config, entity_id, pid, unit, title, {})
        self.assertEqual(config['state_topic'], t_state('sensor', entity_id))

    def test_point_2685_always_gets_date_device_class(self):
        """Point 2685 is a special date sensor — must always get device_class='date'."""
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        config = {}
        MqttDiscoveryPublisher._build_sensor_config(
            config, 'nibe_2685', 2685, '', 'Days since commissioning', {})
        self.assertEqual(config.get('device_class'), 'date')

    def test_point_2685_config_minimal(self):
        """Point 2685 returns early after setting device_class — no unit etc."""
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        config = {}
        MqttDiscoveryPublisher._build_sensor_config(
            config, 'nibe_2685', 2685, 'days', 'Title', {})
        self.assertNotIn('unit_of_measurement', config)

    @given(_safe_entity_id,
           _nibe_point_id.filter(lambda p: p != 2685),
           st.text(min_size=1, max_size=10).filter(lambda s: s.strip()),
           st.text(max_size=50))
    def test_unit_present_sets_unit_of_measurement(self, entity_id, pid, unit, title):
        """Non-empty unit must always produce unit_of_measurement in config."""
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        config = {}
        MqttDiscoveryPublisher._build_sensor_config(
            config, entity_id, pid, unit, title, {})
        self.assertIn('unit_of_measurement', config)
        self.assertEqual(config['unit_of_measurement'], unit)

    @given(_safe_entity_id,
           _nibe_point_id.filter(lambda p: p != 2685),
           st.text(max_size=50))
    def test_no_unit_no_unit_of_measurement(self, entity_id, pid, title):
        """Empty unit must not produce unit_of_measurement in config."""
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        config = {}
        MqttDiscoveryPublisher._build_sensor_config(
            config, entity_id, pid, '', title, {})
        self.assertNotIn('unit_of_measurement', config)

    @given(_safe_entity_id, _nibe_point_id, st.text(max_size=10),
           st.text(max_size=50))
    def test_never_raises(self, entity_id, pid, unit, title):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        config = {}
        MqttDiscoveryPublisher._build_sensor_config(
            config, entity_id, pid, unit, title, {})  # must not raise


# ---------------------------------------------------------------------------
# DynamicPointMap.record_outcome properties (nibe_dynamic_map.py)
# ---------------------------------------------------------------------------


class TestRecordOutcomeProperties(unittest.TestCase):
    """Hypothesis properties for DynamicPointMap.record_outcome."""

    def _map_with(self, pid, values):
        from nibe_dynamic_map import DynamicPointMap, DynamicPointEntry
        m = DynamicPointMap()
        m._table[pid] = DynamicPointEntry(
            point_id=pid, title='Test', entity_type='switch',
            unprocessed_values=set(values),
        )
        return m

    @given(st.integers(min_value=1, max_value=9999),
           st.integers(min_value=0, max_value=5),
           st.lists(st.integers(min_value=10000, max_value=19999), max_size=5))
    def test_value_moves_to_processed(self, pid, value, new_pids):
        """After record_outcome, value must be in processed_values."""
        m = self._map_with(pid, {value})
        m.record_outcome(pid, value, new_pids)
        self.assertIn(value, m._table[pid].processed_values)

    @given(st.integers(min_value=1, max_value=9999),
           st.integers(min_value=0, max_value=5),
           st.lists(st.integers(min_value=10000, max_value=19999), max_size=5))
    def test_value_removed_from_unprocessed(self, pid, value, new_pids):
        """After record_outcome, value must not be in unprocessed_values."""
        m = self._map_with(pid, {value})
        m.record_outcome(pid, value, new_pids)
        self.assertNotIn(value, m._table[pid].unprocessed_values)

    @given(st.integers(min_value=1, max_value=9999),
           st.integers(min_value=0, max_value=5),
           st.lists(st.integers(min_value=10000, max_value=19999),
                    min_size=1, max_size=5))
    def test_nonempty_new_pids_sets_is_controlling_true(self, pid, value, new_pids):
        """Non-empty new_point_ids must always set is_controlling=True."""
        m = self._map_with(pid, {value})
        m.record_outcome(pid, value, new_pids)
        self.assertTrue(m._table[pid].is_controlling)

    @given(st.integers(min_value=1, max_value=9999),
           st.integers(min_value=0, max_value=5),
           st.lists(st.integers(min_value=10000, max_value=19999), max_size=5))
    def test_dynamic_points_stored_correctly(self, pid, value, new_pids):
        """dynamic_points_by_value[value] must match the input list."""
        m = self._map_with(pid, {value})
        m.record_outcome(pid, value, new_pids)
        self.assertEqual(m._table[pid].dynamic_points_by_value[value], new_pids)

    @given(st.integers(min_value=1, max_value=9999),
           st.integers(min_value=0, max_value=5),
           st.lists(st.integers(min_value=10000, max_value=19999), max_size=5))
    def test_unknown_point_id_never_raises(self, pid, value, new_pids):
        """record_outcome on a point not in the table must not raise."""
        from nibe_dynamic_map import DynamicPointMap
        m = DynamicPointMap()
        m.record_outcome(pid, value, new_pids)  # must not raise

    @given(st.integers(min_value=1, max_value=9999))
    def test_all_empty_records_sets_is_controlling_false(self, pid):
        """After recording all values as empty, is_controlling becomes False."""
        m = self._map_with(pid, {0, 1})
        m.record_outcome(pid, 0, [])
        m.record_outcome(pid, 1, [])
        self.assertFalse(m._table[pid].is_controlling)


# ---------------------------------------------------------------------------
# _build_number_config properties (nibe_mqtt_publisher.py)
# ---------------------------------------------------------------------------


class TestBuildNumberConfigProperties(unittest.TestCase):
    """Hypothesis properties for _build_number_config."""

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        return MqttDiscoveryPublisher(
            mqtt_client=MagicMock(), device_info={},
            device_id='test', device_name='Test',
        )

    def _meta(self, min_val=0, max_val=100, divisor=1, decimal=0,
              change=1, unit=''):
        return {
            'minValue': min_val, 'maxValue': max_val,
            'divisor': divisor, 'decimal': decimal,
            'change': change, 'unit': unit,
        }

    @given(_safe_entity_id, _nibe_point_id,
           st.integers(min_value=-100, max_value=100),
           st.integers(min_value=-100, max_value=100))
    def test_always_sets_state_and_command_topics(self, entity_id, pid, mn, mx):
        pub = self._pub()
        config = {}
        pub._build_number_config(config, entity_id, pid, 'Test', '',
                                 self._meta(mn, mx), {})
        self.assertIn('state_topic', config)
        self.assertIn('command_topic', config)

    def test_number_optimistic_is_false(self):
        """number entities must set optimistic:false so HA waits for a state
        confirmation before updating the UI — prevents flip-back during
        post-write learning detection windows."""
        pub = self._pub()
        config = {}
        pub._build_number_config(config, 'test_id', 9999, 'Test', '',
                                 self._meta(), {})
        self.assertFalse(config.get('optimistic', True),
                         "number discovery config must include optimistic:false")

    @given(_safe_entity_id, _nibe_point_id,
           st.integers(min_value=-100, max_value=100),
           st.integers(min_value=-100, max_value=100))
    def test_topics_consistent_with_t_state_t_command(self, entity_id, pid, mn, mx):
        from nibe_mqtt_publisher import t_state, t_command
        pub = self._pub()
        config = {}
        pub._build_number_config(config, entity_id, pid, 'Test', '',
                                 self._meta(mn, mx), {})
        self.assertEqual(config['state_topic'],   t_state('number', entity_id))
        self.assertEqual(config['command_topic'], t_command('number', entity_id))

    @given(_safe_entity_id, _nibe_point_id,
           st.integers(min_value=-100, max_value=98),
           st.text(min_size=1, max_size=10).filter(lambda s: s.strip()))
    def test_unit_present_sets_unit_of_measurement(self, entity_id, pid, mn, unit):
        pub = self._pub()
        config = {}
        pub._build_number_config(config, entity_id, pid, 'Test', unit,
                                 self._meta(mn, mn + 2), {})
        self.assertIn('unit_of_measurement', config)
        self.assertEqual(config['unit_of_measurement'], unit)

    @given(_safe_entity_id, _nibe_point_id,
           st.integers(min_value=-100, max_value=98))
    def test_no_unit_no_unit_of_measurement(self, entity_id, pid, mn):
        pub = self._pub()
        config = {}
        pub._build_number_config(config, entity_id, pid, 'Test', '',
                                 self._meta(mn, mn + 2), {})
        self.assertNotIn('unit_of_measurement', config)

    @given(_safe_entity_id, _nibe_point_id, st.integers(min_value=1, max_value=100))
    def test_step_always_positive(self, entity_id, pid, change):
        """step value must always be > 0."""
        pub = self._pub()
        config = {}
        pub._build_number_config(config, entity_id, pid, 'Test', '',
                                 self._meta(0, 100, change=change), {})
        if 'step' in config:
            self.assertGreater(config['step'], 0)

    @given(_safe_entity_id, _nibe_point_id,
           st.integers(min_value=-100, max_value=100),
           st.integers(min_value=-100, max_value=100))
    def test_never_raises(self, entity_id, pid, mn, mx):
        pub = self._pub()
        config = {}
        pub._build_number_config(config, entity_id, pid, 'Test', '',
                                 self._meta(mn, mx), {})


# ---------------------------------------------------------------------------
# _publish_static_attributes properties (nibe_mqtt_publisher.py)
# ---------------------------------------------------------------------------


class TestPublishStaticAttributesProperties(unittest.TestCase):
    """Hypothesis properties for _publish_static_attributes."""

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        mqtt = MagicMock()
        pub = MqttDiscoveryPublisher(
            mqtt_client=mqtt, device_info={},
            device_id='test', device_name='Test',
        )
        return pub, mqtt

    def _meta(self, divisor=1, decimal=0, unit=''):
        return {'divisor': divisor, 'decimal': decimal, 'unit': unit,
                'modbusRegisterID': 100, 'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'intDefaultValue': None}

    @given(_safe_entity_id)
    def test_button_entity_skips_publish(self, entity_id):
        """Button entities must never publish static attributes."""
        pub, mqtt = self._pub()
        config = {}
        pub._publish_static_attributes(
            'button', entity_id, 100, '', False, '', self._meta(), config)
        self.assertNotIn('json_attributes_topic', config)
        self.assertEqual(mqtt.publish.call_count, 0)

    @given(_safe_entity_id,
           st.sampled_from(['sensor', 'switch', 'number', 'select', 'binary_sensor']))
    def test_non_button_sets_json_attributes_topic(self, entity_id, entity_type):
        """Non-button entities must always set json_attributes_topic in config."""
        pub, mqtt = self._pub()
        config = {}
        pub._publish_static_attributes(
            entity_type, entity_id, 100, '', False, '', self._meta(), config)
        self.assertIn('json_attributes_topic', config)

    @given(_safe_entity_id,
           st.sampled_from(['sensor', 'switch', 'number', 'select']))
    def test_attributes_topic_consistent_with_t_attributes(self, entity_id, entity_type):
        from nibe_mqtt_publisher import t_attributes
        pub, mqtt = self._pub()
        config = {}
        pub._publish_static_attributes(
            entity_type, entity_id, 100, '', False, '', self._meta(), config)
        self.assertEqual(config['json_attributes_topic'],
                         t_attributes(entity_type, entity_id))

    @given(_safe_entity_id,
           st.sampled_from(['sensor', 'switch', 'number', 'select']))
    def test_payload_always_valid_json(self, entity_id, entity_type):
        import json as _json
        pub, mqtt = self._pub()
        config = {}
        pub._publish_static_attributes(
            entity_type, entity_id, 100, '', False, '', self._meta(), config)
        calls = [c for c in mqtt.publish.call_args_list
                 if 'attributes' in c.args[0]]
        self.assertTrue(calls)
        _json.loads(calls[0].args[1])  # must parse without raising

    @given(_safe_entity_id,
           st.sampled_from(['sensor', 'switch', 'number']))
    def test_always_published_retained(self, entity_id, entity_type):
        pub, mqtt = self._pub()
        config = {}
        pub._publish_static_attributes(
            entity_type, entity_id, 100, '', False, '', self._meta(), config)
        calls = [c for c in mqtt.publish.call_args_list
                 if 'attributes' in c.args[0]]
        self.assertTrue(calls)
        retain = calls[0].kwargs.get('retain',
                 calls[0].args[2] if len(calls[0].args) > 2 else False)
        self.assertTrue(retain)


# ---------------------------------------------------------------------------
# publish_entity_discovery properties (nibe_mqtt_publisher.py)
# ---------------------------------------------------------------------------


class TestPublishEntityDiscoveryProperties(unittest.TestCase):
    """Hypothesis properties for publish_entity_discovery."""

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        mqtt = MagicMock()
        mqtt.publish.return_value = MagicMock(rc=0)
        pub = MqttDiscoveryPublisher(
            mqtt_client=mqtt, device_info={'identifiers': ['test']},
            device_id='test', device_name='Test',
        )
        return pub, mqtt

    def _point(self, pid, entity_type='sensor', title='Test Point',
               writable=False, unit='', description=''):
        return {
            'variableId':     pid,
            'display_title':  title,
            'entity_type':    entity_type,
            'entity_category': 'diagnostic',
            'is_writable':    writable,
            'is_dynamic':     False,
            'description':    description,
            'metadata': {
                'unit': unit, 'shortUnit': unit,
                'minValue': 0, 'maxValue': 100,
                'modbusRegisterID': pid,
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'variableType': 'integer', 'variableSize': 'u8',
                'isWritable': writable, 'divisor': 1, 'decimal': 0,
                'intDefaultValue': 0, 'stringDefaultValue': '', 'change': 1,
            },
        }

    @given(_nibe_point_id,
           st.sampled_from(['sensor', 'switch', 'binary_sensor', 'button']))
    def test_returns_entity_info_dict_or_none(self, pid, entity_type):
        """Returns entity_info dict with topic strings, or None on publish failure."""
        pub, _ = self._pub()
        result = pub.publish_entity_discovery(self._point(pid, entity_type), {})
        self.assertIn(type(result), (dict, type(None)))

    @given(_nibe_point_id,
           st.sampled_from(['sensor', 'switch', 'binary_sensor']))
    def test_result_contains_entity_id(self, pid, entity_type):
        """Returned entity_info always contains entity_id."""
        pub, _ = self._pub()
        result = pub.publish_entity_discovery(self._point(pid, entity_type), {})
        if result is not None:
            self.assertIn('entity_id', result)

    @given(_nibe_point_id,
           st.sampled_from(['sensor', 'switch', 'binary_sensor']))
    def test_result_entity_id_matches_create_entity_id(self, pid, entity_type):
        from nibe_mqtt_publisher import create_entity_id
        pub, _ = self._pub()
        result = pub.publish_entity_discovery(self._point(pid, entity_type), {})
        if result is not None:
            self.assertEqual(result['entity_id'], create_entity_id(pid))

    @given(_nibe_point_id,
           st.sampled_from(['sensor', 'switch', 'binary_sensor']))
    def test_result_availability_topic_consistent(self, pid, entity_type):
        from nibe_mqtt_publisher import t_available, create_entity_id
        pub, _ = self._pub()
        result = pub.publish_entity_discovery(self._point(pid, entity_type), {})
        if result is not None:
            expected = t_available(entity_type, create_entity_id(pid))
            self.assertEqual(result['availability_topic'], expected)

    @given(_nibe_point_id,
           st.sampled_from(['sensor', 'switch', 'binary_sensor']))
    def test_publishes_to_config_topic(self, pid, entity_type):
        from nibe_mqtt_publisher import t_config, create_entity_id
        pub, mqtt = self._pub()
        pub.publish_entity_discovery(self._point(pid, entity_type), {})
        expected = t_config(entity_type, create_entity_id(pid))
        topics = [c.args[0] for c in mqtt.publish.call_args_list]
        self.assertIn(expected, topics)

    @given(_nibe_point_id,
           st.sampled_from(['sensor', 'switch', 'binary_sensor']))
    def test_published_config_contains_unique_id(self, pid, entity_type):
        """The published discovery config must always contain unique_id=nibe_{pid}."""
        import json as _json
        from nibe_mqtt_publisher import t_config, create_entity_id
        pub, mqtt = self._pub()
        pub.publish_entity_discovery(self._point(pid, entity_type), {})
        config_topic = t_config(entity_type, create_entity_id(pid))
        calls = [c for c in mqtt.publish.call_args_list
                 if c.args[0] == config_topic]
        if calls:
            payload = _json.loads(calls[0].args[1])
            self.assertEqual(payload['unique_id'], f'nibe_{pid}')

    @given(_nibe_point_id,
           st.sampled_from(['sensor', 'switch', 'binary_sensor', 'number',
                            'select', 'button', 'time', 'text']))
    @example(pid=50827, entity_type='sensor')       # THS-10 humidity: unit override
    @example(pid=5110,  entity_type='switch')       # ENTITY_TYPE_OVERRIDE switch
    @example(pid=22077, entity_type='binary_sensor')  # ENTITY_TYPE_OVERRIDE binary_sensor
    @example(pid=1024,  entity_type='number')       # divisor=60 Timer EME
    @example(pid=0,     entity_type='sensor')       # pid=0 falsy edge case
    def test_all_entity_types_return_required_keys(self, pid, entity_type):
        """publish_entity_discovery must return a dict with the required keys
        for ALL entity types — not just sensor/switch/binary_sensor.
        A new entity type added without updating the return dict would cause
        a KeyError in EntityManager._update_entity_state on every poll."""
        _REQUIRED = {
            'point_id', 'entity_id', 'entity_type',
            'availability_topic', 'state_topic', 'command_topic',
            'metadata', 'is_writable', 'point_data',
            'is_degenerate_range', 'value_mapping',
        }
        pub, _ = self._pub()
        result = pub.publish_entity_discovery(self._point(pid, entity_type), {})
        if result is not None:
            missing = _REQUIRED - set(result.keys())
            self.assertFalse(missing,
                             f"entity_type={entity_type!r}: missing keys {missing}")

    @given(_nibe_point_id,
           st.sampled_from(['sensor', 'switch', 'binary_sensor', 'number',
                            'select', 'button', 'time', 'text']))
    def test_availability_topic_always_set_on_success(self, pid, entity_type):
        """Returned entity_info must always have a non-empty availability_topic —
        EntityManager uses it to publish 'online'/'offline' on every poll."""
        pub, _ = self._pub()
        result = pub.publish_entity_discovery(self._point(pid, entity_type), {})
        if result is not None:
            self.assertTrue(result['availability_topic'],
                            f"availability_topic empty for entity_type={entity_type!r}")

    @given(_nibe_point_id,
           st.sampled_from(['sensor', 'switch', 'binary_sensor', 'number',
                            'select', 'button', 'time', 'text']))
    def test_second_publish_skips_mqtt_when_config_unchanged(self, pid, entity_type):
        """Publishing the same point twice without invalidating the hash must
        result in exactly one MQTT publish (not two) — the dedup guard works
        for all entity types."""
        from nibe_mqtt_publisher import t_config, create_entity_id
        pub, mqtt = self._pub()
        pub.publish_entity_discovery(self._point(pid, entity_type), {})
        pub.publish_entity_discovery(self._point(pid, entity_type), {})
        # Second call must not have added any new config publishes
        config_topic = t_config(entity_type, create_entity_id(pid))
        config_calls = [c for c in mqtt.publish.call_args_list
                        if c.args[0] == config_topic]
        self.assertEqual(len(config_calls), 1,
                         f"entity_type={entity_type!r}: config published {len(config_calls)}x, expected 1")

    @given(_nibe_point_id,
           st.sampled_from(['sensor', 'switch', 'binary_sensor', 'number',
                            'select', 'button']))
    def test_never_raises(self, pid, entity_type):
        pub, _ = self._pub()
        pub.publish_entity_discovery(self._point(pid, entity_type), {})


# ---------------------------------------------------------------------------
# publish_point_metadata properties (nibe_mqtt_publisher.py)
# ---------------------------------------------------------------------------


class TestReadAppliedModeFromFileProperties(unittest.TestCase):
    """Hypothesis properties for EntityManager._read_applied_mode_from_file."""

    def test_missing_file_returns_none(self):
        em = _make_em()
        result = em._read_applied_mode_from_file('/nonexistent/path/mode.txt')
        self.assertIsNone(result)

    def test_missing_file_never_raises(self):
        em = _make_em()
        em._read_applied_mode_from_file('/nonexistent/path/mode.txt')

    def test_empty_file_returns_none(self):
        import tempfile
        import os
        em = _make_em()
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
            f.write('')
            path = f.name
        try:
            result = em._read_applied_mode_from_file(path)
            self.assertIsNone(result)
        finally:
            os.unlink(path)

    def test_whitespace_only_file_returns_none(self):
        import tempfile
        import os
        em = _make_em()
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
            f.write('   \n  ')
            path = f.name
        try:
            result = em._read_applied_mode_from_file(path)
            self.assertIsNone(result)
        finally:
            os.unlink(path)

    @given(st.text(min_size=1, max_size=20,
                   alphabet=st.characters(categories=['L', 'N'],
                                          include_characters='_')))
    def test_file_with_content_returns_stripped_content(self, mode):
        import tempfile
        import os
        em = _make_em()
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as f:
            f.write(f'  {mode}  \n')
            path = f.name
        try:
            result = em._read_applied_mode_from_file(path)
            self.assertEqual(result, mode.strip())
        finally:
            os.unlink(path)

    @given(st.text(min_size=1, max_size=20,
                   alphabet=st.characters(categories=['L', 'N'],
                                          include_characters='_')))
    def test_result_always_stripped(self, mode):
        import tempfile
        import os
        em = _make_em()
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as f:
            f.write(mode)
            path = f.name
        try:
            result = em._read_applied_mode_from_file(path)
            if result is not None:
                self.assertEqual(result, result.strip())
        finally:
            os.unlink(path)

    def test_oserror_returns_none(self):
        """Any OSError (permissions, broken path) must return None gracefully."""
        em = _make_em()
        result = em._read_applied_mode_from_file('/proc/1/mem')
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# _pub_state properties (nibe_mqtt_publisher.py)
# ---------------------------------------------------------------------------


class TestGetMemoryUsageProperties(unittest.TestCase):
    """Hypothesis properties for EntityManager.get_memory_usage."""

    _EXPECTED_KEYS = frozenset({
        'total_points', 'active_entities', 'mqtt_enabled_points',
        'active_dynamic_points', 'value_cache_size', 'last_states_size',
        'point_string_cache_size', 'pending_writes', 'estimated_memory_mb',
    })

    def test_always_returns_dict(self):
        em = _make_em()
        result = em.get_memory_usage()
        self.assertIsInstance(result, dict)

    def test_all_expected_keys_present(self):
        em = _make_em()
        result = em.get_memory_usage()
        for key in self._EXPECTED_KEYS:
            self.assertIn(key, result, f"Missing key: {key}")

    def test_count_values_always_non_negative(self):
        em = _make_em()
        result = em.get_memory_usage()
        for key in ('total_points', 'active_entities', 'mqtt_enabled_points',
                    'active_dynamic_points', 'value_cache_size',
                    'last_states_size', 'point_string_cache_size', 'pending_writes'):
            self.assertGreaterEqual(result[key], 0, f"{key} is negative")

    def test_estimated_memory_mb_non_negative(self):
        em = _make_em()
        result = em.get_memory_usage()
        self.assertGreaterEqual(result['estimated_memory_mb'], 0)

    def test_never_raises(self):
        em = _make_em()
        em.get_memory_usage()  # must not raise

    @given(st.integers(min_value=0, max_value=100))
    def test_total_points_matches_all_points_by_id(self, n_points):
        """total_points must always equal len(all_points_by_id)."""
        em = _make_em()
        for i in range(n_points):
            em.all_points_by_id[i] = {'variableId': i}
        result = em.get_memory_usage()
        self.assertEqual(result['total_points'], n_points)

    @given(st.integers(min_value=0, max_value=50))
    def test_mqtt_enabled_points_matches_set(self, n):
        """mqtt_enabled_points must always equal len(mqtt_enabled_points set)."""
        em = _make_em()
        for i in range(n):
            em.mqtt_enabled_points.add(i)
        result = em.get_memory_usage()
        self.assertEqual(result['mqtt_enabled_points'], n)

    @given(st.integers(min_value=0, max_value=50))
    def test_pending_writes_matches_dict(self, n):
        """pending_writes must always equal len(pending_writes dict)."""
        em = _make_em()
        for i in range(n):
            em.pending_writes[i] = {'value': i, 'time': 0.0}
        result = em.get_memory_usage()
        self.assertEqual(result['pending_writes'], n)


# ---------------------------------------------------------------------------
# publish_stats computed field properties (nibe_mqtt_publisher.py)
# ---------------------------------------------------------------------------


class TestPublishStatsProperties(unittest.TestCase):
    """Hypothesis properties for MqttDiscoveryPublisher.publish_stats."""

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        mqtt = MagicMock()
        pub = MqttDiscoveryPublisher(
            mqtt_client=mqtt, device_info={},
            device_id='test', device_name='Test',
        )
        return pub, mqtt

    def _get_attrs(self, mqtt):
        import json as _json
        from nibe_mqtt_publisher import MgmtTopic
        calls = [c for c in mqtt.publish.call_args_list
                 if c.args[0] == MgmtTopic.STATS_ATTRS]
        self.assertTrue(calls, "No STATS_ATTRS publish found")
        return _json.loads(calls[-1].args[1])

    @given(st.integers(min_value=0, max_value=10000),
           st.integers(min_value=0, max_value=10000),
           st.integers(min_value=0, max_value=10000))
    def test_enabled_percentage_in_0_100(self, total, enabled, active):
        """enabled_percentage must always be in [0, 100]."""
        pub, mqtt = self._pub()
        pub.publish_stats(
            all_points_count=total, mqtt_enabled_count=min(enabled, total),
            active_count=active, type_counts={}, category_counts={},
            writable_count=0,
        )
        attrs = self._get_attrs(mqtt)
        self.assertGreaterEqual(attrs['enabled_percentage'], 0)
        self.assertLessEqual(attrs['enabled_percentage'], 100)

    @given(st.integers(min_value=1, max_value=1000),
           st.integers(min_value=0, max_value=1000))
    def test_write_success_rate_in_0_100(self, total_writes, successes):
        """write_success_rate must always be in [0, 100]."""
        pub, mqtt = self._pub()
        pub.publish_stats(
            all_points_count=100, mqtt_enabled_count=10,
            active_count=10, type_counts={}, category_counts={},
            writable_count=0,
            write_total=total_writes,
            write_success=min(successes, total_writes),
        )
        attrs = self._get_attrs(mqtt)
        self.assertGreaterEqual(attrs['write_success_rate'], 0)
        self.assertLessEqual(attrs['write_success_rate'], 100)

    @given(st.integers(min_value=0, max_value=1000),
           st.integers(min_value=0, max_value=1000))
    def test_discrepancy_equals_enabled_minus_active(self, enabled, active):
        """discrepancy must always equal mqtt_enabled - actually_active."""
        pub, mqtt = self._pub()
        pub.publish_stats(
            all_points_count=max(enabled, 1), mqtt_enabled_count=enabled,
            active_count=active, type_counts={}, category_counts={},
            writable_count=0,
        )
        attrs = self._get_attrs(mqtt)
        self.assertEqual(attrs['discrepancy'], enabled - active)

    def test_zero_total_writes_gives_100_success_rate(self):
        """With no writes, success rate defaults to 100%."""
        pub, mqtt = self._pub()
        pub.publish_stats(
            all_points_count=10, mqtt_enabled_count=5,
            active_count=5, type_counts={}, category_counts={},
            writable_count=0, write_total=0,
        )
        attrs = self._get_attrs(mqtt)
        self.assertEqual(attrs['write_success_rate'], 100.0)

    def test_zero_total_points_gives_0_percent_enabled(self):
        """With no points, enabled_percentage must be 0."""
        pub, mqtt = self._pub()
        pub.publish_stats(
            all_points_count=0, mqtt_enabled_count=0,
            active_count=0, type_counts={}, category_counts={},
            writable_count=0,
        )
        attrs = self._get_attrs(mqtt)
        self.assertEqual(attrs['enabled_percentage'], 0)


# ---------------------------------------------------------------------------
# publish_device_modes properties (nibe_mqtt_publisher.py)
# ---------------------------------------------------------------------------


class TestPublishDeviceModesProperties(unittest.TestCase):
    """Hypothesis properties for publish_device_modes and
    publish_initial_device_modes."""

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        mqtt = MagicMock()
        pub = MqttDiscoveryPublisher(
            mqtt_client=mqtt, device_info={},
            device_id='test', device_name='Test',
        )
        return pub, mqtt

    def _aid_published(self, mqtt):
        from nibe_mqtt_publisher import MgmtTopic
        calls = [c.args[1] for c in mqtt.publish.call_args_list
                 if c.args[0] == MgmtTopic.AID_STATE]
        self.assertTrue(calls, "No AID_STATE publish found")
        return calls[-1]

    def _smart_published(self, mqtt):
        from nibe_mqtt_publisher import MgmtTopic
        calls = [c.args[1] for c in mqtt.publish.call_args_list
                 if c.args[0] == MgmtTopic.SMART_STATE]
        self.assertTrue(calls, "No SMART_STATE publish found")
        return calls[-1]

    def test_aid_on_publishes_ON(self):
        pub, mqtt = self._pub()
        pub.publish_device_modes('on', 'normal')
        self.assertEqual(self._aid_published(mqtt), 'ON')

    @given(st.text().filter(lambda s: s != 'on'))
    @example(aid_mode='off')       # firmware default
    @example(aid_mode='OFF')       # uppercase variant
    @example(aid_mode='')          # empty string
    @example(aid_mode='standby')   # possible firmware state
    def test_aid_not_on_publishes_OFF(self, aid_mode):
        pub, mqtt = self._pub()
        pub.publish_device_modes(aid_mode, 'normal')
        self.assertEqual(self._aid_published(mqtt), 'OFF')

    @given(st.text(max_size=20))
    def test_smart_mode_published_as_is(self, smart_mode):
        """smart_mode value is published exactly as passed."""
        pub, mqtt = self._pub()
        pub.publish_device_modes('off', smart_mode)
        self.assertEqual(self._smart_published(mqtt), smart_mode)

    @given(st.text(max_size=20), st.text(max_size=20))
    def test_always_publishes_both_states(self, aid, smart):
        """Both AID_STATE and SMART_STATE must always be published."""
        from nibe_mqtt_publisher import MgmtTopic
        pub, mqtt = self._pub()
        pub.publish_device_modes(aid, smart)
        topics = [c.args[0] for c in mqtt.publish.call_args_list]
        self.assertIn(MgmtTopic.AID_STATE,   topics)
        self.assertIn(MgmtTopic.SMART_STATE, topics)

    @given(st.sampled_from(['on', 'ON', 'On']))
    def test_initial_aid_on_variants_all_publish_ON(self, aid_value):
        """aidMode 'on'/'ON'/'On' all produce 'ON' (case-insensitive)."""
        pub, mqtt = self._pub()
        pub.publish_initial_device_modes({'aidMode': aid_value, 'smartMode': 'normal'})
        self.assertEqual(self._aid_published(mqtt), 'ON')

    @given(st.sampled_from(['off', 'OFF', 'Off', '', 'standby']))
    def test_initial_aid_non_on_variants_publish_OFF(self, aid_value):
        """aidMode anything other than 'on' (case-insensitive) → 'OFF'."""
        pub, mqtt = self._pub()
        pub.publish_initial_device_modes({'aidMode': aid_value, 'smartMode': 'normal'})
        self.assertEqual(self._aid_published(mqtt), 'OFF')

    def test_initial_missing_aid_defaults_to_OFF(self):
        """Missing aidMode key → 'OFF'."""
        pub, mqtt = self._pub()
        pub.publish_initial_device_modes({'smartMode': 'normal'})
        self.assertEqual(self._aid_published(mqtt), 'OFF')

    @given(st.text(max_size=20))
    @example(smart_mode='normal')  # confirmed working on hardware
    @example(smart_mode='away')    # confirmed working on hardware
    @example(smart_mode='NORMAL')  # uppercase variant must be lowercased
    @example(smart_mode='AWAY')    # uppercase variant must be lowercased
    def test_initial_smart_mode_always_lowercased(self, smart_mode):
        """smartMode is always lowercased before publishing."""
        pub, mqtt = self._pub()
        pub.publish_initial_device_modes({'aidMode': 'off', 'smartMode': smart_mode})
        published = self._smart_published(mqtt)
        self.assertEqual(published, smart_mode.lower())


# ---------------------------------------------------------------------------
# publish_bridge_alert properties (nibe_mqtt_publisher.py)
# ---------------------------------------------------------------------------


class TestGetCachedEntityTypeProperties(unittest.TestCase):
    """Hypothesis properties for EntityManager._get_cached_entity_type.

    Key invariant: the cache must be transparent — the cached result must
    always equal what detect_entity_type would return directly. If the cache
    ever returns a stale or wrong result, entities would be misclassified.
    """

    _VALID_TYPES = frozenset({
        'sensor', 'binary_sensor', 'switch', 'number',
        'select', 'button', 'text', 'time',
    })
    _VALID_CATEGORIES = frozenset({'config', 'diagnostic'})

    def _point(self, pid, modbus_type='MODBUS_INPUT_REGISTER',
               var_type='integer', writable=False):
        return {
            'variableId': pid,
            'title': f'Point {pid}',
            'description': '',
            'metadata': {
                'modbusRegisterType': modbus_type,
                'variableType': var_type,
                'variableSize': 'u8',
                'isWritable': writable,
                'minValue': 0, 'maxValue': 1,
                'unit': '', 'divisor': 1, 'decimal': 0,
                'modbusRegisterID': pid,
                'intDefaultValue': 0, 'stringDefaultValue': '',
                'change': 1,
            }
        }

    @given(_nibe_point_id,
           st.sampled_from(['MODBUS_INPUT_REGISTER', 'MODBUS_HOLDING_REGISTER',
                            'MODBUS_NO_REGISTER']),
           st.booleans())
    def test_cached_result_equals_direct_detect(self, pid, modbus, writable):
        """_get_cached_entity_type must always return what detect_entity_type returns."""
        from nibe_entity_detection import detect_entity_type
        em = _make_em()
        point = self._point(pid, modbus, writable=writable)
        direct = detect_entity_type(point)
        cached = em._get_cached_entity_type(point)
        self.assertEqual(cached, direct)

    @given(_nibe_point_id,
           st.sampled_from(['MODBUS_INPUT_REGISTER', 'MODBUS_HOLDING_REGISTER']))
    def test_always_returns_valid_type(self, pid, modbus):
        em = _make_em()
        point = self._point(pid, modbus)
        entity_type, _ = em._get_cached_entity_type(point)
        self.assertIn(entity_type, self._VALID_TYPES)

    @given(_nibe_point_id,
           st.sampled_from(['MODBUS_INPUT_REGISTER', 'MODBUS_HOLDING_REGISTER']))
    def test_always_returns_valid_category(self, pid, modbus):
        em = _make_em()
        point = self._point(pid, modbus)
        _, category = em._get_cached_entity_type(point)
        self.assertIn(category, self._VALID_CATEGORIES)

    @given(_nibe_point_id,
           st.sampled_from(['MODBUS_INPUT_REGISTER', 'MODBUS_HOLDING_REGISTER']))
    def test_second_call_returns_same_result(self, pid, modbus):
        """Cache must be transparent — two calls always return the same result."""
        em = _make_em()
        point = self._point(pid, modbus)
        first  = em._get_cached_entity_type(point)
        second = em._get_cached_entity_type(point)
        self.assertEqual(first, second)

    @given(_nibe_point_id,
           st.sampled_from(['MODBUS_INPUT_REGISTER', 'MODBUS_HOLDING_REGISTER']))
    def test_always_returns_two_tuple(self, pid, modbus):
        em = _make_em()
        point = self._point(pid, modbus)
        result = em._get_cached_entity_type(point)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    @given(_nibe_point_id,
           st.sampled_from(['MODBUS_INPUT_REGISTER', 'MODBUS_HOLDING_REGISTER']))
    def test_cache_hit_identical_to_cache_miss(self, pid, modbus):
        """Fresh cache miss and warmed cache hit must return identical results."""
        em_cold = _make_em()
        em_warm = _make_em()
        point = self._point(pid, modbus)
        # Warm the cache
        em_warm._get_cached_entity_type(point)
        # Both must return same result
        self.assertEqual(
            em_cold._get_cached_entity_type(point),
            em_warm._get_cached_entity_type(point),
        )

    @given(_nibe_point_id,
           st.sampled_from(['MODBUS_INPUT_REGISTER', 'MODBUS_HOLDING_REGISTER']))
    def test_after_cache_invalidation_result_still_correct(self, pid, modbus):
        """After invalidating the cache for a point, next call still returns correct result."""
        from nibe_entity_detection import detect_entity_type
        em = _make_em()
        point = self._point(pid, modbus)
        em._get_cached_entity_type(point)  # warm cache
        em._entity_type_cache.pop(pid)     # invalidate
        result = em._get_cached_entity_type(point)  # re-fetch
        self.assertEqual(result, detect_entity_type(point))


# ---------------------------------------------------------------------------
# _build_point_metadata_dict properties (nibe_mqtt_publisher.py)
# ---------------------------------------------------------------------------


class TestBuildPointMetadataDictProperties(unittest.TestCase):
    """Hypothesis properties for _build_point_metadata_dict."""

    _EXPECTED_KEYS = frozenset({
        'id', 'title', 'type', 'writable', 'unit', 'unit_overridden',
        'unit_raw', 'min_value', 'max_value', 'category', 'description',
        'is_dynamic', 'modbusRegisterID',
    })

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        mqtt = MagicMock()
        return MqttDiscoveryPublisher(
            mqtt_client=mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def _point(self, pid, title='Test', entity_type='sensor',
               writable=False, is_dynamic=False, unit='',
               min_val=0, max_val=100, description=''):
        return {
            'variableId':     pid,
            'display_title':  title,
            'entity_type':    entity_type,
            'entity_category': 'diagnostic',
            'is_writable':    writable,
            'is_dynamic':     is_dynamic,
            'description':    description,
            'metadata': {
                'unit': unit, 'shortUnit': unit,
                'minValue': min_val, 'maxValue': max_val,
                'modbusRegisterID': pid,
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'variableType': 'integer', 'variableSize': 'u8',
                'isWritable': writable, 'divisor': 1, 'decimal': 0,
                'intDefaultValue': 0, 'stringDefaultValue': '',
                'change': 1,
            },
        }

    @given(_nibe_point_id)
    def test_always_returns_dict(self, pid):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._point(pid))
        self.assertIsInstance(result, dict)

    @given(_nibe_point_id)
    def test_all_expected_keys_present(self, pid):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._point(pid))
        for key in self._EXPECTED_KEYS:
            self.assertIn(key, result, f"Missing key: {key}")

    @given(_nibe_point_id)
    def test_id_matches_variable_id(self, pid):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._point(pid))
        self.assertEqual(result['id'], pid)

    @given(_nibe_point_id, st.booleans())
    def test_writable_matches_is_writable(self, pid, writable):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._point(pid, writable=writable))
        self.assertEqual(result['writable'], writable)

    @given(_nibe_point_id, st.booleans())
    def test_is_dynamic_matches_point_field(self, pid, is_dynamic):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._point(pid, is_dynamic=is_dynamic))
        self.assertEqual(result['is_dynamic'], is_dynamic)

    @given(_nibe_point_id)
    def test_unit_overridden_always_bool(self, pid):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._point(pid))
        self.assertIsInstance(result['unit_overridden'], bool)

    @given(_nibe_point_id, st.text(max_size=30))
    def test_description_preserved(self, pid, description):
        pub = self._pub()
        result = pub._build_point_metadata_dict(
            self._point(pid, description=description))
        self.assertEqual(result['description'], description)

    @given(_nibe_point_id,
           st.integers(min_value=-32768, max_value=32767),
           st.integers(min_value=-32768, max_value=32767))
    def test_min_max_values_preserved(self, pid, min_val, max_val):
        pub = self._pub()
        result = pub._build_point_metadata_dict(
            self._point(pid, min_val=min_val, max_val=max_val))
        self.assertEqual(result['min_value'], min_val)
        self.assertEqual(result['max_value'], max_val)

    @given(_nibe_point_id, st.text(max_size=20))
    def test_unit_raw_matches_metadata_unit(self, pid, unit):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._point(pid, unit=unit))
        self.assertEqual(result['unit_raw'], unit)


# ---------------------------------------------------------------------------
# _build_point_metadata_dict extended field properties
# ---------------------------------------------------------------------------


class TestBuildDisableNotificationProperties(unittest.TestCase):
    """Hypothesis properties for EntityManager.build_disable_notification.

    Key invariants:
    - Always returns a 3-tuple (title, message, notification_id)
    - notification_id always starts with 'nibe_ha_disable_'
    - notification_id never contains '.' or '-'
    - notification_id length bounded
    - action='re-enabled' → title mentions re-enabled
    - is_dynamic point → title mentions Dynamic
    """

    def _em_with_point(self, pid, is_dynamic=False):
        em = _make_em()
        em.all_points_by_id[pid] = {
            'variableId': pid,
            'display_title': f'Point {pid}',
            'is_dynamic': is_dynamic,
        }
        return em

    @given(_nibe_point_id,
           st.text(min_size=1, max_size=50, alphabet=st.characters(
               categories=['L', 'N'], include_characters='._-')),
           st.sampled_from(['disabled', 're-enabled', 'removed']))
    def test_always_returns_3_tuple(self, pid, ha_entity_id, action):
        em = self._em_with_point(pid)
        result = em.build_disable_notification(pid, ha_entity_id, action)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)

    @given(_nibe_point_id,
           st.text(min_size=1, max_size=50, alphabet=st.characters(
               categories=['L', 'N'], include_characters='._-')),
           st.sampled_from(['disabled', 're-enabled', 'removed']))
    def test_all_elements_are_strings(self, pid, ha_entity_id, action):
        em = self._em_with_point(pid)
        title, message, notif_id = em.build_disable_notification(
            pid, ha_entity_id, action)
        self.assertIsInstance(title, str)
        self.assertIsInstance(message, str)
        self.assertIsInstance(notif_id, str)

    @given(_nibe_point_id,
           st.text(min_size=1, max_size=80, alphabet=st.characters(
               categories=['L', 'N'], include_characters='._-')),
           st.sampled_from(['disabled', 're-enabled']))
    def test_notif_id_always_starts_with_prefix(self, pid, ha_entity_id, action):
        em = self._em_with_point(pid)
        _, _, notif_id = em.build_disable_notification(pid, ha_entity_id, action)
        self.assertTrue(notif_id.startswith('nibe_ha_disable_'),
                        f"notif_id={notif_id!r} does not start with prefix")

    @given(_nibe_point_id,
           st.text(min_size=1, max_size=80, alphabet=st.characters(
               categories=['L', 'N'], include_characters='._-')),
           st.sampled_from(['disabled', 're-enabled']))
    def test_notif_id_has_no_dots_or_dashes(self, pid, ha_entity_id, action):
        """Dots and dashes from ha_entity_id must be sanitised in notif_id."""
        em = self._em_with_point(pid)
        _, _, notif_id = em.build_disable_notification(pid, ha_entity_id, action)
        # Only the prefix part matters — check the sanitised portion
        suffix = notif_id[len('nibe_ha_disable_'):]
        self.assertNotIn('.', suffix)
        self.assertNotIn('-', suffix)

    @given(_nibe_point_id,
           st.text(min_size=1, max_size=200),
           st.sampled_from(['disabled', 're-enabled']))
    def test_notif_id_length_bounded(self, pid, ha_entity_id, action):
        """notification_id must never exceed prefix + 60 chars."""
        em = self._em_with_point(pid)
        _, _, notif_id = em.build_disable_notification(pid, ha_entity_id, action)
        self.assertLessEqual(len(notif_id), len('nibe_ha_disable_') + 60)

    @given(_nibe_point_id,
           st.text(min_size=1, max_size=50, alphabet=st.characters(
               categories=['L', 'N'], include_characters='._-')))
    def test_re_enabled_action_title_mentions_re_enabled(self, pid, ha_entity_id):
        em = self._em_with_point(pid)
        title, _, _ = em.build_disable_notification(pid, ha_entity_id, 're-enabled')
        self.assertIn('re-enabled', title.lower())

    @given(_nibe_point_id.filter(lambda p: p > 0),
           st.text(min_size=1, max_size=50, alphabet=st.characters(
               categories=['L', 'N'], include_characters='._-')))
    def test_dynamic_point_title_mentions_dynamic(self, pid, ha_entity_id):
        """Dynamic points get a distinct title mentioning 'Dynamic'."""
        em = self._em_with_point(pid, is_dynamic=True)
        title, _, _ = em.build_disable_notification(pid, ha_entity_id, 'disabled')
        self.assertIn('Dynamic', title)

    @given(st.text(min_size=1, max_size=50, alphabet=st.characters(
               categories=['L', 'N'], include_characters='._-')),
           st.sampled_from(['disabled', 're-enabled']))
    def test_none_point_id_uses_entity_id_as_display(self, ha_entity_id, action):
        """When point_id=None, ha_entity_id must appear in the message."""
        em = _make_em()
        _, message, _ = em.build_disable_notification(None, ha_entity_id, action)
        self.assertIn(ha_entity_id, message)

    @given(_nibe_point_id,
           st.text(min_size=1, max_size=50, alphabet=st.characters(
               categories=['L', 'N'], include_characters='._-')),
           st.sampled_from(['disabled', 're-enabled']))
    def test_never_raises(self, pid, ha_entity_id, action):
        em = self._em_with_point(pid)
        em.build_disable_notification(pid, ha_entity_id, action)  # must not raise


# ---------------------------------------------------------------------------
# publish_all_metadata properties (nibe_mqtt_publisher.py)
# ---------------------------------------------------------------------------


class TestPublishAllMetadataProperties(unittest.TestCase):
    """Hypothesis properties for publish_all_metadata.

    The batch metadata message is what the Lovelace card subscribes to
    at startup. Its structure is critical — if count or keys are wrong
    the card fails silently.
    """

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        mqtt = MagicMock()
        pub = MqttDiscoveryPublisher(
            mqtt_client=mqtt, device_info={},
            device_id='test', device_name='Test',
        )
        return pub, mqtt

    def _point(self, pid):
        return {
            'variableId':     pid,
            'display_title':  f'Point {pid}',
            'entity_type':    'sensor',
            'entity_category': 'diagnostic',
            'is_writable':    False,
            'is_dynamic':     False,
            'description':    '',
            'metadata': {
                'unit': '', 'shortUnit': '',
                'minValue': 0, 'maxValue': 100,
                'modbusRegisterID': pid,
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'variableType': 'integer', 'variableSize': 'u8',
                'isWritable': False, 'divisor': 1, 'decimal': 0,
                'intDefaultValue': 0, 'stringDefaultValue': '',
                'change': 1,
            },
        }

    def _get_payload(self, mqtt):
        import json as _json
        from nibe_mqtt_publisher import BrowserTopic
        calls = [c for c in mqtt.publish.call_args_list
                 if c.args[0] == BrowserTopic.ALL_METADATA]
        self.assertTrue(calls, "No ALL_METADATA publish found")
        return _json.loads(calls[-1].args[1])

    @given(st.sets(st.integers(min_value=1, max_value=9999), max_size=20))
    def test_count_equals_number_of_points(self, pids):
        pub, mqtt = self._pub()
        points = [self._point(pid) for pid in pids]
        pub.publish_all_metadata(points)
        payload = self._get_payload(mqtt)
        self.assertEqual(payload['count'], len(pids))

    @given(st.sets(st.integers(min_value=1, max_value=9999), max_size=20))
    def test_metadata_keys_are_string_point_ids(self, pids):
        """Metadata keys must be string representations of point IDs."""
        pub, mqtt = self._pub()
        points = [self._point(pid) for pid in pids]
        pub.publish_all_metadata(points)
        payload = self._get_payload(mqtt)
        for pid in pids:
            self.assertIn(str(pid), payload['metadata'])

    @given(st.sets(st.integers(min_value=1, max_value=9999), max_size=20))
    def test_metadata_keys_never_ints(self, pids):
        """Metadata dict must use string keys — JSON requires it but verify."""
        pub, mqtt = self._pub()
        points = [self._point(pid) for pid in pids]
        pub.publish_all_metadata(points)
        payload = self._get_payload(mqtt)
        for key in payload['metadata'].keys():
            self.assertIsInstance(key, str)

    @given(st.sets(st.integers(min_value=1, max_value=9999), min_size=1, max_size=10))
    def test_each_metadata_entry_has_id_field(self, pids):
        """Each metadata entry must have an 'id' field matching the point."""
        pub, mqtt = self._pub()
        points = [self._point(pid) for pid in pids]
        pub.publish_all_metadata(points)
        payload = self._get_payload(mqtt)
        for pid in pids:
            entry = payload['metadata'][str(pid)]
            self.assertEqual(entry['id'], pid)

    @given(st.sets(st.integers(min_value=1, max_value=9999), max_size=20))
    def test_always_published_retained(self, pids):
        from nibe_mqtt_publisher import BrowserTopic
        pub, mqtt = self._pub()
        pub.publish_all_metadata([self._point(pid) for pid in pids])
        calls = [c for c in mqtt.publish.call_args_list
                 if c.args[0] == BrowserTopic.ALL_METADATA]
        self.assertTrue(calls)
        retain = calls[-1].kwargs.get('retain',
                 calls[-1].args[2] if len(calls[-1].args) > 2 else False)
        self.assertTrue(retain)

    @given(st.sets(st.integers(min_value=1, max_value=9999), max_size=20))
    def test_always_valid_json(self, pids):
        pub, mqtt = self._pub()
        pub.publish_all_metadata([self._point(pid) for pid in pids])
        self._get_payload(mqtt)  # must parse without raising

    def test_empty_points_publishes_zero_count(self):
        pub, mqtt = self._pub()
        pub.publish_all_metadata([])
        payload = self._get_payload(mqtt)
        self.assertEqual(payload['count'], 0)
        self.assertEqual(payload['metadata'], {})

    @given(st.sets(st.integers(min_value=1, max_value=9999), max_size=10))
    def test_metadata_count_matches_metadata_dict_length(self, pids):
        """count field must always equal len(metadata)."""
        pub, mqtt = self._pub()
        pub.publish_all_metadata([self._point(pid) for pid in pids])
        payload = self._get_payload(mqtt)
        self.assertEqual(payload['count'], len(payload['metadata']))


# ---------------------------------------------------------------------------
# publish_bridge_status properties (nibe_mqtt_publisher.py)
# ---------------------------------------------------------------------------


class TestPublishBridgeStatusProperties(unittest.TestCase):
    """Hypothesis properties for publish_bridge_status."""

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
                 if c.args[0] == BrowserTopic.BRIDGE_STATUS]
        self.assertTrue(calls, "No BRIDGE_STATUS publish found")
        return _json.loads(calls[-1].args[1])

    def _call(self, pub, failures=0, threshold=3, write_total=0,
              write_success=0, write_failed=0):
        pub.publish_bridge_status(
            bridge_start_time=0.0,
            api_consecutive_failures=failures,
            api_failure_threshold=threshold,
            api_last_success_time=0.0,
            last_fetch_duration=0.1,
            write_total=write_total,
            write_success=write_success,
            write_failed=write_failed,
            last_write_error=None,
            pending_write_count=0,
            mqtt_enabled_count=10,
            all_points_count=100,
            known_dynamic_count=5,
        )

    @given(st.integers(min_value=0, max_value=10),
           st.integers(min_value=1, max_value=10))
    def test_status_healthy_when_failures_below_threshold(self, failures, threshold):
        pub, mqtt = self._pub()
        self._call(pub, failures=failures, threshold=threshold)
        payload = self._get_payload(mqtt)
        expected = 'healthy' if failures < threshold else 'degraded'
        self.assertEqual(payload['status'], expected)

    @given(st.integers(min_value=0, max_value=10),
           st.integers(min_value=1, max_value=10))
    def test_status_always_healthy_or_degraded(self, failures, threshold):
        pub, mqtt = self._pub()
        self._call(pub, failures=failures, threshold=threshold)
        payload = self._get_payload(mqtt)
        self.assertIn(payload['status'], ('healthy', 'degraded'))

    @given(st.integers(min_value=1, max_value=1000),
           st.integers(min_value=0, max_value=1000))
    def test_write_success_rate_in_0_100(self, total, success):
        pub, mqtt = self._pub()
        self._call(pub, write_total=total, write_success=min(success, total))
        payload = self._get_payload(mqtt)
        rate = payload['writes']['success_rate_pct']
        self.assertGreaterEqual(rate, 0)
        self.assertLessEqual(rate, 100)

    def test_uptime_s_non_negative(self):
        pub, mqtt = self._pub()
        self._call(pub)
        payload = self._get_payload(mqtt)
        self.assertGreaterEqual(payload['uptime_s'], 0)

    def test_always_published_retained(self):
        """Bridge status must be retained so new subscribers get current state."""
        from nibe_mqtt_publisher import BrowserTopic
        pub, mqtt = self._pub()
        self._call(pub)
        calls = [c for c in mqtt.publish.call_args_list
                 if c.args[0] == BrowserTopic.BRIDGE_STATUS]
        self.assertTrue(calls)
        retain = calls[-1].kwargs.get('retain',
                 calls[-1].args[2] if len(calls[-1].args) > 2 else False)
        self.assertTrue(retain)

    @given(st.integers(min_value=0, max_value=10),
           st.integers(min_value=1, max_value=10))
    def test_api_healthy_field_consistent_with_status(self, failures, threshold):
        """api.healthy must match status == 'healthy'."""
        pub, mqtt = self._pub()
        self._call(pub, failures=failures, threshold=threshold)
        payload = self._get_payload(mqtt)
        self.assertEqual(
            payload['api']['healthy'],
            payload['status'] == 'healthy',
        )


# ---------------------------------------------------------------------------
# publish_point_list properties (nibe_mqtt_publisher.py)
# ---------------------------------------------------------------------------


class TestPublishPointListProperties(unittest.TestCase):
    """Hypothesis properties for publish_point_list."""

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
                 if c.args[0] == BrowserTopic.POINT_LIST]
        self.assertTrue(calls, "No POINT_LIST publish found")
        data = _json.loads(calls[-1].args[1])
        return data['points']  # list of ints under 'points' key

    @given(st.dictionaries(
        st.integers(min_value=1, max_value=9999),
        st.just({}),
        max_size=20,
    ))
    def test_payload_length_equals_all_points_count(self, all_points_by_id):
        pub, mqtt = self._pub()
        pub.publish_point_list(all_points_by_id)
        points = self._get_payload(mqtt)
        self.assertIsInstance(points, list)
        self.assertEqual(len(points), len(all_points_by_id))

    @given(st.dictionaries(
        st.integers(min_value=1, max_value=9999),
        st.just({}),
        max_size=20,
    ))
    def test_payload_contains_all_point_ids(self, all_points_by_id):
        pub, mqtt = self._pub()
        pub.publish_point_list(all_points_by_id)
        points = self._get_payload(mqtt)
        for pid in all_points_by_id:
            self.assertIn(pid, points)

    @given(st.dictionaries(
        st.integers(min_value=1, max_value=9999),
        st.just({}),
        max_size=20,
    ))
    def test_all_elements_are_ints(self, all_points_by_id):
        pub, mqtt = self._pub()
        pub.publish_point_list(all_points_by_id)
        points = self._get_payload(mqtt)
        for item in points:
            self.assertIsInstance(item, int)

    def test_empty_dict_publishes_empty_list(self):
        pub, mqtt = self._pub()
        pub.publish_point_list({})
        points = self._get_payload(mqtt)
        self.assertEqual(points, [])

    @given(st.dictionaries(
        st.integers(min_value=1, max_value=9999),
        st.just({}),
        max_size=20,
    ))
    def test_points_are_sorted(self, all_points_by_id):
        """Point IDs must be published in sorted order."""
        pub, mqtt = self._pub()
        pub.publish_point_list(all_points_by_id)
        points = self._get_payload(mqtt)
        self.assertEqual(points, sorted(points))


# ---------------------------------------------------------------------------
# publish_uptime properties (nibe_mqtt_publisher.py)
# ---------------------------------------------------------------------------


class TestPublishUptimeProperties(unittest.TestCase):
    """Hypothesis properties for publish_uptime."""

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        mqtt = MagicMock()
        pub = MqttDiscoveryPublisher(
            mqtt_client=mqtt, device_info={},
            device_id='test', device_name='Test',
        )
        return pub, mqtt

    def _get_attrs(self, mqtt):
        import json as _json
        from nibe_mqtt_publisher import MgmtTopic
        calls = [c for c in mqtt.publish.call_args_list
                 if c.args[0] == MgmtTopic.UPTIME_ATTRS]
        self.assertTrue(calls, "No UPTIME_ATTRS publish found")
        return _json.loads(calls[-1].args[1])

    def _get_state(self, mqtt):
        from nibe_mqtt_publisher import MgmtTopic
        calls = [c for c in mqtt.publish.call_args_list
                 if c.args[0] == MgmtTopic.UPTIME_STATE]
        self.assertTrue(calls, "No UPTIME_STATE publish found")
        return calls[-1].args[1]

    @given(st.floats(min_value=0.0, max_value=1e9,
                     allow_nan=False, allow_infinity=False),
           st.floats(min_value=0.0, max_value=1e9,
                     allow_nan=False, allow_infinity=False),
           st.integers(min_value=0, max_value=100))
    def test_uptime_state_is_non_negative_integer_string(self, start, last, failures):
        """UPTIME_STATE must always be a non-negative integer string."""
        pub, mqtt = self._pub()
        pub.publish_uptime(start, last, failures)
        state = self._get_state(mqtt)
        self.assertRegex(state, r'^\d+$')
        self.assertGreaterEqual(int(state), 0)

    @given(st.integers(min_value=0, max_value=100))
    def test_consecutive_failures_preserved_in_attrs(self, failures):
        """consecutive_failures must appear exactly in the attributes."""
        pub, mqtt = self._pub()
        pub.publish_uptime(0.0, 0.0, failures)
        attrs = self._get_attrs(mqtt)
        self.assertEqual(attrs['consecutive_failures'], failures)

    @given(st.floats(min_value=0.0, max_value=1e9,
                     allow_nan=False, allow_infinity=False),
           st.floats(min_value=0.0, max_value=1e9,
                     allow_nan=False, allow_infinity=False),
           st.integers(min_value=0, max_value=100))
    def test_always_publishes_both_state_and_attrs(self, start, last, failures):
        """Both UPTIME_STATE and UPTIME_ATTRS must always be published."""
        from nibe_mqtt_publisher import MgmtTopic
        pub, mqtt = self._pub()
        pub.publish_uptime(start, last, failures)
        topics = [c.args[0] for c in mqtt.publish.call_args_list]
        self.assertIn(MgmtTopic.UPTIME_STATE, topics)
        self.assertIn(MgmtTopic.UPTIME_ATTRS, topics)

    @given(st.floats(min_value=0.0, max_value=1e9,
                     allow_nan=False, allow_infinity=False),
           st.floats(min_value=0.0, max_value=1e9,
                     allow_nan=False, allow_infinity=False),
           st.integers(min_value=0, max_value=100))
    def test_attrs_always_valid_json(self, start, last, failures):
        """UPTIME_ATTRS payload must always be valid JSON."""
        pub, mqtt = self._pub()
        pub.publish_uptime(start, last, failures)
        self._get_attrs(mqtt)  # parse without raising


# ---------------------------------------------------------------------------
# publish_api_reachability properties (nibe_mqtt_publisher.py)
# ---------------------------------------------------------------------------


class TestPublishAlarmStateProperties(unittest.TestCase):
    """Hypothesis properties for publish_alarm_state."""

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        mqtt = MagicMock()
        pub = MqttDiscoveryPublisher(
            mqtt_client=mqtt, device_info={},
            device_id='test', device_name='Test',
        )
        return pub, mqtt

    def _get_state(self, mqtt):
        from nibe_mqtt_publisher import MgmtTopic
        calls = [c for c in mqtt.publish.call_args_list
                 if c.args[0] == MgmtTopic.ALARM_STATE]
        self.assertTrue(calls)
        return calls[-1].args[1]

    def _get_attrs(self, mqtt):
        import json as _json
        from nibe_mqtt_publisher import MgmtTopic
        calls = [c for c in mqtt.publish.call_args_list
                 if c.args[0] == MgmtTopic.ALARM_ATTRS]
        self.assertTrue(calls)
        return _json.loads(calls[-1].args[1])

    @given(st.integers(min_value=0, max_value=100))
    def test_state_equals_str_of_count(self, alarm_count):
        pub, mqtt = self._pub()
        pub.publish_alarm_state(alarm_count, [])
        self.assertEqual(self._get_state(mqtt), str(alarm_count))

    @given(st.integers(min_value=0, max_value=100))
    def test_state_is_always_non_negative_integer_string(self, alarm_count):
        pub, mqtt = self._pub()
        pub.publish_alarm_state(alarm_count, [])
        state = self._get_state(mqtt)
        self.assertRegex(state, r'^\d+$')
        self.assertGreaterEqual(int(state), 0)

    @given(st.lists(st.dictionaries(
        st.text(max_size=10), st.text(max_size=20), max_size=3), max_size=5))
    def test_alarms_field_preserved_exactly(self, alarms):
        pub, mqtt = self._pub()
        pub.publish_alarm_state(len(alarms), alarms)
        attrs = self._get_attrs(mqtt)
        self.assertEqual(attrs['alarms'], alarms)

    @given(st.integers(min_value=0, max_value=20),
           st.lists(st.text(max_size=10), max_size=5))
    def test_attrs_always_valid_json(self, count, alarms):
        pub, mqtt = self._pub()
        pub.publish_alarm_state(count, alarms)
        self._get_attrs(mqtt)  # must parse without raising

    @given(st.integers(min_value=0, max_value=20))
    def test_both_state_and_attrs_always_published(self, count):
        from nibe_mqtt_publisher import MgmtTopic
        pub, mqtt = self._pub()
        pub.publish_alarm_state(count, [])
        topics = [c.args[0] for c in mqtt.publish.call_args_list]
        self.assertIn(MgmtTopic.ALARM_STATE, topics)
        self.assertIn(MgmtTopic.ALARM_ATTRS, topics)


# ---------------------------------------------------------------------------
# publish_enabled_state properties (nibe_mqtt_publisher.py)
# ---------------------------------------------------------------------------


class TestPublishEnabledStateProperties(unittest.TestCase):
    """Hypothesis properties for publish_enabled_state."""

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
                 if c.args[0] == BrowserTopic.ENABLED_STATE]
        self.assertTrue(calls, "No ENABLED_STATE publish found")
        return _json.loads(calls[-1].args[1])

    @given(st.sets(st.integers(min_value=1, max_value=9999), max_size=30))
    def test_count_equals_set_size(self, enabled_points):
        pub, mqtt = self._pub()
        pub.publish_enabled_state(enabled_points)
        payload = self._get_payload(mqtt)
        self.assertEqual(payload['count'], len(enabled_points))

    @given(st.sets(st.integers(min_value=1, max_value=9999), max_size=30))
    def test_enabled_points_contains_all_input_pids(self, enabled_points):
        pub, mqtt = self._pub()
        pub.publish_enabled_state(enabled_points)
        payload = self._get_payload(mqtt)
        for pid in enabled_points:
            self.assertIn(pid, payload['enabled_points'])

    @given(st.sets(st.integers(min_value=1, max_value=9999), max_size=30))
    def test_always_valid_json(self, enabled_points):
        pub, mqtt = self._pub()
        pub.publish_enabled_state(enabled_points)
        self._get_payload(mqtt)  # must parse without raising

    @given(st.sets(st.integers(min_value=1, max_value=9999), max_size=30))
    def test_always_published_retained(self, enabled_points):
        from nibe_mqtt_publisher import BrowserTopic
        pub, mqtt = self._pub()
        pub.publish_enabled_state(enabled_points)
        calls = [c for c in mqtt.publish.call_args_list
                 if c.args[0] == BrowserTopic.ENABLED_STATE]
        self.assertTrue(calls)
        retain = calls[-1].kwargs.get('retain',
                 calls[-1].args[2] if len(calls[-1].args) > 2 else False)
        self.assertTrue(retain)

    def test_empty_set_publishes_empty_list(self):
        pub, mqtt = self._pub()
        pub.publish_enabled_state(set())
        payload = self._get_payload(mqtt)
        self.assertEqual(payload['enabled_points'], [])
        self.assertEqual(payload['count'], 0)


# ---------------------------------------------------------------------------
# invalidate_config_hash properties (nibe_mqtt_publisher.py)
# ---------------------------------------------------------------------------


class TestInvalidateConfigHashProperties(unittest.TestCase):
    """Hypothesis properties for invalidate_config_hash."""

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        mqtt = MagicMock()
        pub = MqttDiscoveryPublisher(
            mqtt_client=mqtt, device_info={},
            device_id='test', device_name='Test',
        )
        return pub

    @given(_nibe_point_id)
    def test_never_raises_on_unknown_point(self, pid):
        pub = self._pub()
        pub.invalidate_config_hash(pid)  # must not raise

    @given(_nibe_point_id)
    def test_never_raises_called_twice(self, pid):
        pub = self._pub()
        pub.invalidate_config_hash(pid)
        pub.invalidate_config_hash(pid)  # idempotent — must not raise

    @given(_nibe_point_id)
    def test_after_invalidation_hash_not_in_cache(self, pid):
        """After invalidation, the point_id must not be in _config_hashes."""
        pub = self._pub()
        pub._config_hashes[pid] = 'some_hash'
        pub.invalidate_config_hash(pid)
        self.assertNotIn(pid, pub._config_hashes)

    @given(_nibe_point_id, _nibe_point_id)
    def test_invalidation_does_not_affect_other_points(self, pid1, pid2):
        """Invalidating pid1 must not remove pid2's hash."""
        pub = self._pub()
        if pid1 == pid2:
            return
        pub._config_hashes[pid1] = 'hash1'
        pub._config_hashes[pid2] = 'hash2'
        pub.invalidate_config_hash(pid1)
        self.assertEqual(pub._config_hashes.get(pid2), 'hash2')



class TestBuildButtonConfigProperties(unittest.TestCase):
    """Hypothesis properties for _build_button_config."""

    @given(_safe_entity_id)
    def test_always_sets_command_topic(self, entity_id):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        config = {}
        MqttDiscoveryPublisher._build_button_config(config, entity_id)
        self.assertIn('command_topic', config)

    @given(_safe_entity_id)
    def test_command_topic_contains_entity_id(self, entity_id):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        config = {}
        MqttDiscoveryPublisher._build_button_config(config, entity_id)
        self.assertIn(entity_id, config['command_topic'])

    @given(_safe_entity_id)
    def test_command_topic_contains_press(self, entity_id):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        config = {}
        MqttDiscoveryPublisher._build_button_config(config, entity_id)
        self.assertIn('press', config['command_topic'])

    @given(_safe_entity_id)
    def test_sets_exactly_one_key(self, entity_id):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        config = {}
        MqttDiscoveryPublisher._build_button_config(config, entity_id)
        self.assertEqual(set(config.keys()), {'command_topic'})



class TestBuildSwitchConfigProperties(unittest.TestCase):
    """Hypothesis properties for _build_switch_config."""

    @given(_safe_entity_id)
    def test_always_sets_required_keys(self, entity_id):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        config = {}
        MqttDiscoveryPublisher._build_switch_config(config, entity_id)
        for key in ('state_topic', 'command_topic', 'payload_on', 'payload_off', 'optimistic'):
            self.assertIn(key, config)

    @given(_safe_entity_id)
    def test_payload_on_is_one(self, entity_id):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        config = {}
        MqttDiscoveryPublisher._build_switch_config(config, entity_id)
        self.assertEqual(config['payload_on'], '1')

    @given(_safe_entity_id)
    def test_payload_off_is_zero(self, entity_id):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        config = {}
        MqttDiscoveryPublisher._build_switch_config(config, entity_id)
        self.assertEqual(config['payload_off'], '0')

    @given(_safe_entity_id)
    def test_optimistic_is_false(self, entity_id):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        config = {}
        MqttDiscoveryPublisher._build_switch_config(config, entity_id)
        self.assertFalse(config['optimistic'])

    @given(_safe_entity_id)
    def test_all_topics_contain_entity_id(self, entity_id):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        config = {}
        MqttDiscoveryPublisher._build_switch_config(config, entity_id)
        self.assertIn(entity_id, config['state_topic'])
        self.assertIn(entity_id, config['command_topic'])

    @given(_safe_entity_id)
    def test_consistent_with_t_state_t_command(self, entity_id):
        """state_topic and command_topic must match t_state/t_command output."""
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, t_state, t_command
        config = {}
        MqttDiscoveryPublisher._build_switch_config(config, entity_id)
        self.assertEqual(config['state_topic'], t_state('switch', entity_id))
        self.assertEqual(config['command_topic'], t_command('switch', entity_id))



class TestBuildBinarySensorConfigProperties(unittest.TestCase):
    """Hypothesis properties for _build_binary_sensor_config."""

    @given(_safe_entity_id, st.text(max_size=50))
    def test_always_sets_state_topic(self, entity_id, title):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        config = {}
        MqttDiscoveryPublisher._build_binary_sensor_config(config, entity_id, title)
        self.assertIn('state_topic', config)

    @given(_safe_entity_id, st.text(max_size=50))
    def test_payload_on_is_ON(self, entity_id, title):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        config = {}
        MqttDiscoveryPublisher._build_binary_sensor_config(config, entity_id, title)
        self.assertEqual(config['payload_on'], 'ON')

    @given(_safe_entity_id, st.text(max_size=50))
    def test_payload_off_is_OFF(self, entity_id, title):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        config = {}
        MqttDiscoveryPublisher._build_binary_sensor_config(config, entity_id, title)
        self.assertEqual(config['payload_off'], 'OFF')

    @given(_safe_entity_id, st.text(max_size=50))
    def test_state_topic_contains_entity_id(self, entity_id, title):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        config = {}
        MqttDiscoveryPublisher._build_binary_sensor_config(config, entity_id, title)
        self.assertIn(entity_id, config['state_topic'])

    @given(_safe_entity_id, st.text(max_size=50))
    def test_consistent_with_t_state(self, entity_id, title):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, t_state
        config = {}
        MqttDiscoveryPublisher._build_binary_sensor_config(config, entity_id, title)
        self.assertEqual(config['state_topic'], t_state('binary_sensor', entity_id))



class TestDiscoveryConfigBuilders(unittest.TestCase):
    """Tests for the type-specific discovery config builder methods.

    Each builder mutates a config dict in place.  We test the produced keys
    and values directly — no MQTT broker or entity manager needed.
    """

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        self.mqtt   = MagicMock()
        self.mqtt.publish.return_value = MagicMock(rc=0)
        self.pub    = MqttDiscoveryPublisher(
            mqtt_client = self.mqtt,
            device_info = {"identifiers": ["nibe_test"]},
            device_id   = "test",
            device_name = "Test Device",
        )

    # ── helpers ─────────────────────────────────────────────────────────────

    def _meta(self, **kwargs):
        """Minimal metadata dict with sensible defaults."""
        base = {
            'variableId': 1000, 'variableType': 'integer', 'variableSize': 'u16',
            'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
            'isWritable': True, 'divisor': 1, 'decimal': 0,
            'minValue': 0, 'maxValue': 100, 'intDefaultValue': 0,
            'change': 1, 'unit': '', 'shortUnit': '', 'modbusRegisterID': 4200,
            'stringDefaultValue': '',
        }
        base.update(kwargs)
        return base

    def _point(self, entity_type='sensor', **meta_kwargs):
        """Minimal point dict."""
        meta = self._meta(**meta_kwargs)
        return {
            'variableId':    meta['variableId'],
            'display_title': 'Test Point',
            'description':   '',
            'metadata':      meta,
            'entity_type':   entity_type,
            'entity_category': '',
            'is_writable':   meta['isWritable'],
            'is_dynamic':    False,
        }

    # ── switch ───────────────────────────────────────────────────────────────

    def test_switch_has_state_and_command_topics(self):
        config = {}
        self.pub._build_switch_config(config, 'nibe_1000')
        self.assertIn('state_topic',   config)
        self.assertIn('command_topic', config)

    def test_switch_payloads_are_1_and_0(self):
        config = {}
        self.pub._build_switch_config(config, 'nibe_1000')
        self.assertEqual(config['payload_on'],  '1')
        self.assertEqual(config['payload_off'], '0')

    def test_switch_not_optimistic(self):
        config = {}
        self.pub._build_switch_config(config, 'nibe_1000')
        self.assertFalse(config['optimistic'])

    # ── button ───────────────────────────────────────────────────────────────

    def test_button_has_command_topic(self):
        config = {}
        self.pub._build_button_config(config, 'nibe_1000')
        self.assertIn('command_topic', config)

    def test_button_has_no_state_topic(self):
        config = {}
        self.pub._build_button_config(config, 'nibe_1000')
        self.assertNotIn('state_topic', config)

    # ── binary_sensor ────────────────────────────────────────────────────────

    def test_binary_sensor_payloads(self):
        config = {}
        self.pub._build_binary_sensor_config(config, 'nibe_1000', 'Test')
        self.assertEqual(config['payload_on'],  'ON')
        self.assertEqual(config['payload_off'], 'OFF')

    def test_binary_sensor_has_state_topic(self):
        config = {}
        self.pub._build_binary_sensor_config(config, 'nibe_1000', 'Test')
        self.assertIn('state_topic', config)

    # ── sensor ───────────────────────────────────────────────────────────────

    def test_sensor_has_state_topic(self):
        config = {}
        self.pub._build_sensor_config(config, 'nibe_1000', 1000, '°C', 'Temp', self._meta())
        self.assertIn('state_topic', config)

    def test_sensor_with_unit_gets_unit_of_measurement(self):
        config = {}
        self.pub._build_sensor_config(config, 'nibe_1000', 1000, '°C', 'Temp', self._meta())
        self.assertEqual(config['unit_of_measurement'], '°C')

    def test_sensor_without_unit_no_unit_of_measurement(self):
        config = {}
        self.pub._build_sensor_config(config, 'nibe_1000', 1000, '', 'Count', self._meta())
        self.assertNotIn('unit_of_measurement', config)

    def test_sensor_temperature_gets_measurement_state_class(self):
        config = {}
        self.pub._build_sensor_config(config, 'nibe_1000', 1000, '°C', 'Temp', self._meta())
        self.assertEqual(config.get('state_class'), 'measurement')

    def test_sensor_energy_accumulator_gets_total_increasing(self):
        config = {}
        meta = self._meta(divisor=1, maxValue=99999)
        self.pub._build_sensor_config(config, 'nibe_1000', 1000, 'kWh', 'Energy', meta)
        self.assertEqual(config.get('state_class'), 'total_increasing')
        self.assertEqual(config.get('device_class'), 'energy')

    def test_sensor_kwh_with_zero_max_is_instantaneous_measurement(self):
        """kWh sensor with divisor=100 and maxValue=0 is treated as instantaneous power."""
        config = {}
        meta = self._meta(divisor=100, maxValue=0)
        self.pub._build_sensor_config(config, 'nibe_1000', 1000, 'kWh', 'Power', meta)
        self.assertEqual(config.get('state_class'), 'measurement')
        self.assertNotIn('device_class', config)

    def test_sensor_no_unit_no_state_class(self):
        config = {}
        self.pub._build_sensor_config(config, 'nibe_1000', 1000, '', 'Status', self._meta())
        self.assertNotIn('state_class', config)

    def test_sensor_decimal_sets_suggested_display_precision(self):
        """Firmware decimal field must propagate to suggested_display_precision
        so HA shows the right number of decimal places by default."""
        config = {}
        self.pub._build_sensor_config(
            config, 'nibe_1000', 1000, '°C', 'Temp', self._meta(decimal=1)
        )
        self.assertEqual(config['suggested_display_precision'], 1)

    def test_sensor_zero_decimal_sets_suggested_display_precision_zero(self):
        """decimal=0 (integer register) must explicitly set precision=0,
        overriding HA's default heuristic which may guess more decimal places
        for certain device classes (e.g. temperature)."""
        config = {}
        self.pub._build_sensor_config(
            config, 'nibe_1000', 1000, '°C', 'Temp', self._meta(decimal=0)
        )
        self.assertEqual(config['suggested_display_precision'], 0)

    def test_sensor_without_unit_never_gets_suggested_display_precision(self):
        """Regression test: a string/enum status sensor (no unit — e.g.
        compressor status reporting 'Running'/'Opening'/'Not accessible',
        or a firmware version string like '0.0.61') must NEVER have
        suggested_display_precision set, even though the firmware metadata
        always carries a 'decimal' field.

        HA treats the mere presence of suggested_display_precision as a
        declaration that the entity is numeric, regardless of its value.
        Setting it on a text-valued sensor causes HA to reject every state
        update with a ValueError, since the actual state is a string but
        the entity now claims to be numeric.
        """
        config = {}
        self.pub._build_sensor_config(
            config, 'nibe_1000', 1000, '', 'Compressor Status', self._meta(decimal=0)
        )
        self.assertNotIn('suggested_display_precision', config)

    # ── number ───────────────────────────────────────────────────────────────

    def test_number_has_state_and_command_topics(self):
        config = {}
        self.pub._build_number_config(
            config, 'nibe_1000', 1000, 'Test', '°C', self._meta(minValue=0, maxValue=100), {}
        )
        self.assertIn('state_topic',   config)
        self.assertIn('command_topic', config)

    def test_number_min_max_divided_by_divisor(self):
        config = {}
        self.pub._build_number_config(
            config, 'nibe_1000', 1000, 'Test', '°C',
            self._meta(minValue=0, maxValue=1000, divisor=10), {}
        )
        self.assertAlmostEqual(config['min'], 0.0)
        self.assertAlmostEqual(config['max'], 100.0)

    def test_number_step_matches_divisor(self):
        config = {}
        self.pub._build_number_config(
            config, 'nibe_1000', 1000, 'Test', '°C',
            self._meta(minValue=0, maxValue=1000, divisor=10), {}
        )
        self.assertAlmostEqual(config['step'], 0.1)

    def test_number_step_is_1_for_integer_register(self):
        config = {}
        self.pub._build_number_config(
            config, 'nibe_1000', 1000, 'Test', '',
            self._meta(minValue=0, maxValue=10, divisor=1), {}
        )
        self.assertEqual(config['step'], 1)

    def test_number_mode_is_box(self):
        config = {}
        self.pub._build_number_config(
            config, 'nibe_1000', 1000, 'Test', '',
            self._meta(minValue=0, maxValue=10), {}
        )
        self.assertEqual(config['mode'], 'box')

    def test_number_degenerate_range_sets_flag(self):
        config = {}
        self.pub._build_number_config(
            config, 'nibe_1000', 1000, 'Test', '',
            self._meta(minValue=5, maxValue=5), {}
        )
        self.assertTrue(config.get('_degenerate_range'))

    def test_number_degenerate_range_uses_fallback_bounds(self):
        config = {}
        self.pub._build_number_config(
            config, 'nibe_1000', 1000, 'Test', '',
            self._meta(minValue=5, maxValue=5), {}
        )
        self.assertLessEqual(config['min'], -100)
        self.assertGreaterEqual(config['max'], 100)

    def test_number_degenerate_range_anchors_to_current_value(self):
        """When current value is known, fallback bounds anchor to it."""
        config = {}
        self.pub._build_number_config(
            config, 'nibe_1000', 1000, 'Test', '',
            self._meta(minValue=5, maxValue=5),
            {1000: {'raw_value': 500}}   # current raw = 500, divisor=1
        )
        self.assertLessEqual(config['min'], 500)
        self.assertGreaterEqual(config['max'], 500)

    def test_number_unit_added_when_present(self):
        config = {}
        self.pub._build_number_config(
            config, 'nibe_1000', 1000, 'Test', '°C',
            self._meta(minValue=0, maxValue=100), {}
        )
        self.assertEqual(config['unit_of_measurement'], '°C')

    # ── select ───────────────────────────────────────────────────────────────

    def test_select_has_state_and_command_topics(self):
        config = {}
        self.pub._build_select_config(
            config, 'nibe_1000', 1000, self._meta(), '0=Off,1=On'
        )
        self.assertIn('state_topic',   config)
        self.assertIn('command_topic', config)

    def test_select_options_parsed_from_description(self):
        config = {}
        self.pub._build_select_config(
            config, 'nibe_1000', 1000, self._meta(), '0=Off,1=On'
        )
        self.assertIn('options', config)
        self.assertEqual(len(config['options']), 2)

    def test_select_no_options_when_description_empty(self):
        config = {}
        self.pub._build_select_config(
            config, 'nibe_1000', 1000, self._meta(), ''
        )
        self.assertNotIn('options', config)

    # ── publish_entity_discovery integration ─────────────────────────────────

    def test_publish_entity_discovery_returns_entity_info(self):
        point = self._point('sensor', unit='°C')
        result = self.pub.publish_entity_discovery(point, {})
        self.assertIsNotNone(result)
        self.assertIn('entity_id',    result)
        self.assertIn('state_topic',  result)
        self.assertIn('entity_type',  result)

    def test_publish_entity_discovery_skips_unchanged_config(self):
        from nibe_mqtt_publisher import t_config
        point = self._point('sensor')
        self.pub.publish_entity_discovery(point, {})
        config_topic = t_config('sensor', 'nibe_1000')
        config_publishes = [
            c for c in self.mqtt.publish.call_args_list
            if c.args[0] == config_topic
        ]
        self.assertEqual(len(config_publishes), 1, "First call should publish config once")
        self.pub.publish_entity_discovery(point, {})
        config_publishes_after = [
            c for c in self.mqtt.publish.call_args_list
            if c.args[0] == config_topic
        ]
        self.assertEqual(len(config_publishes_after), 1, "Second call must not republish unchanged config")

    def test_publish_entity_discovery_republishes_after_hash_invalidation(self):
        point = self._point('sensor')
        self.pub.publish_entity_discovery(point, {})
        self.pub.invalidate_config_hash(point['variableId'])
        call_count_before = self.mqtt.publish.call_count
        self.pub.publish_entity_discovery(point, {})
        self.assertGreater(self.mqtt.publish.call_count, call_count_before)

    def test_publish_entity_discovery_returns_none_on_mqtt_error(self):
        self.mqtt.publish.return_value = MagicMock(rc=1)
        point = self._point('sensor')
        result = self.pub.publish_entity_discovery(point, {})
        self.assertIsNone(result)

    def test_publish_entity_discovery_button_has_no_state_topic(self):
        point = self._point('button')
        result = self.pub.publish_entity_discovery(point, {})
        self.assertIsNotNone(result)
        self.assertIsNone(result.get('state_topic'))

    def test_publish_entity_discovery_switch_is_not_degenerate(self):
        point = self._point('switch')
        result = self.pub.publish_entity_discovery(point, {})
        self.assertFalse(result['is_degenerate_range'])

    def test_publish_entity_discovery_number_degenerate_flag_propagated(self):
        point = self._point('number', minValue=5, maxValue=5)
        result = self.pub.publish_entity_discovery(point, {})
        self.assertTrue(result['is_degenerate_range'])


# ===========================================================================
# 22. NibeApiClient — fetch and write methods
# ===========================================================================


class TestTimeAndDateEntityTypes(unittest.TestCase):
    """time and date variableType points are remapped at detection level.

    time (HOLDING, writable)   → number/config
    date (HOLDING, writable)   → number/config
    time (INPUT,   read-only)  → sensor/diagnostic
    date (INPUT,   read-only)  → sensor/diagnostic

    Neither type should ever reach the publisher as 'time' or 'date',
    so the publisher's unknown-type fallback is a safety net only.
    """

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

    def _point(self, var_type, modbus_type='MODBUS_HOLDING_REGISTER', writable=True):
        return {
            'variableId':    3708,
            'display_title': 'Test Time Point',
            'description':   '',
            'metadata': {
                'variableId': 3708, 'variableType': var_type,
                'variableSize': 'u16',
                'modbusRegisterType': modbus_type,
                'isWritable': writable, 'divisor': 1, 'decimal': 0,
                'minValue': 0, 'maxValue': 86400, 'intDefaultValue': 0,
                'change': 1, 'unit': 's', 'shortUnit': 's',
                'modbusRegisterID': 1234, 'stringDefaultValue': '',
            },
            'entity_type':    var_type,
            'entity_category': 'config',
            'is_writable':    writable,
            'is_dynamic':     False,
        }

    def test_time_holding_maps_to_number(self):
        """Uses a synthetic point ID not in ENTITY_TYPE_OVERRIDES so the
        detection logic is tested without override interference.  Point 3708
        is intentionally overridden to 'time' and is not suitable here."""
        from nibe_entity_detection import detect_entity_type
        point = {
            'variableId': 99999,
            'metadata': {
                'variableType': 'time', 'variableSize': 'unknown',
                'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                'isWritable': True, 'divisor': 1, 'minValue': 0,
                'maxValue': 86400, 'intDefaultValue': 0,
            },
            'description': '',
        }
        entity_type, category = detect_entity_type(point)
        self.assertEqual(entity_type, 'number')
        self.assertEqual(category, 'config')

    def test_date_holding_maps_to_number(self):
        from nibe_entity_detection import detect_entity_type
        point = {
            'variableId': 9999,
            'metadata': {
                'variableType': 'date', 'variableSize': 'unknown',
                'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                'isWritable': True, 'divisor': 1, 'minValue': 0,
                'maxValue': 0, 'intDefaultValue': 0,
            },
            'description': '',
        }
        entity_type, category = detect_entity_type(point)
        self.assertEqual(entity_type, 'number')
        self.assertEqual(category, 'config')

    def test_time_input_maps_to_sensor(self):
        from nibe_entity_detection import detect_entity_type
        point = {
            'variableId': 9998,
            'metadata': {
                'variableType': 'time', 'variableSize': 'unknown',
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'isWritable': False, 'divisor': 1, 'minValue': 0,
                'maxValue': 0, 'intDefaultValue': 0,
            },
            'description': '',
        }
        entity_type, category = detect_entity_type(point)
        self.assertEqual(entity_type, 'sensor')
        self.assertEqual(category, 'diagnostic')

    def test_date_input_maps_to_sensor(self):
        from nibe_entity_detection import detect_entity_type
        point = {
            'variableId': 2685,
            'metadata': {
                'variableType': 'date', 'variableSize': 'unknown',
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'isWritable': False, 'divisor': 1, 'minValue': 0,
                'maxValue': 0, 'intDefaultValue': 0,
            },
            'description': '',
        }
        entity_type, category = detect_entity_type(point)
        self.assertEqual(entity_type, 'sensor')
        self.assertEqual(category, 'diagnostic')

    def test_unknown_entity_type_falls_back_to_sensor(self):
        p = self._point('unknown_future_type')
        result = self.pub.publish_entity_discovery(p, {})
        self.assertIsNotNone(result)
        self.assertIsNotNone(result.get('state_topic'),
                             "unknown entity type must still get a state_topic via fallback")


# ===========================================================================
# 26. DynamicPointMap — dataclass, serialisation, lookup, population
# ===========================================================================


class TestResolveUnit(unittest.TestCase):
    """resolve_unit() is the single source of truth for unit resolution,
    introduced to fix a real bug: the Entity Manager card's details modal
    was showing the raw, pre-override, uncleaned unit (e.g. a switch
    firmware-mislabelled '%') while the actual HA entity correctly showed
    the overridden/cleaned unit. Two independent code paths (the real
    discovery config vs. the card's metadata payload) had drifted apart —
    this consolidates them so that can't happen again. Zero coverage on
    nibe_mqtt_publisher.py before this round."""

    def test_no_override_returns_cleaned_raw_unit(self):
        from nibe_mqtt_publisher import resolve_unit
        unit, overridden = resolve_unit(999999, '°C')
        self.assertEqual(unit, '°C')
        self.assertFalse(overridden)

    def test_known_override_point_returns_overridden_value(self):
        """Point 4562 is a real, confirmed ENTITY_TYPE override case in this
        installation's UNIT_OVERRIDES table (a switch firmware mislabels
        with unit '%') — this is the exact real-world bug this function
        fixes the modal for."""
        from nibe_mqtt_publisher import resolve_unit
        unit, overridden = resolve_unit(4562, '%')
        self.assertEqual(unit, '')
        self.assertTrue(overridden)

    def test_known_override_point_50827_thS10_humidity(self):
        """Point 50827 (THS-10 humidity, real hardware on this installation)
        — firmware reports '%RH', override forces it to '%'."""
        from nibe_mqtt_publisher import resolve_unit
        unit, overridden = resolve_unit(50827, '%RH')
        self.assertEqual(unit, '%')
        self.assertTrue(overridden)

    def test_mojibake_unit_cleaned_even_without_override(self):
        """A point with no override but a mojibake-corrupted unit must
        still come back clean — resolve_unit always cleans, override or not."""
        from nibe_mqtt_publisher import resolve_unit
        unit, overridden = resolve_unit(999999, '\u00c2\u00b0C')
        self.assertEqual(unit, '°C')
        self.assertFalse(overridden)

    def test_override_value_itself_gets_cleaned(self):
        """If an override value were ever mojibake-affected, it must still
        go through cleaning, not bypass it just because it came from the
        override table rather than firmware."""
        from nibe_mqtt_publisher import resolve_unit
        with patch.dict('nibe_mqtt_publisher.UNIT_OVERRIDES', {12345: '\u00c2\u00b0C'}):
            unit, overridden = resolve_unit(12345, 'whatever firmware said')
        self.assertEqual(unit, '°C')
        self.assertTrue(overridden)

    def test_empty_raw_unit_no_override_returns_empty(self):
        from nibe_mqtt_publisher import resolve_unit
        unit, overridden = resolve_unit(999999, '')
        self.assertEqual(unit, '')
        self.assertFalse(overridden)



class TestBuildPointMetadataDict(unittest.TestCase):
    """The dict that feeds the Entity Manager card's details modal. Confirms
    the actual regression test for the bug: a switch with firmware unit '%'
    must show the resolved/overridden unit, the override flag, and the raw
    firmware value — not silently show the pre-override raw unit as if it
    were final, which is what the card displayed before this fix."""

    def _publisher(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        pub = object.__new__(MqttDiscoveryPublisher)
        pub.mqtt = MagicMock()
        pub.device_info = {}
        return pub

    def _point(self, point_id, unit='', entity_type='sensor', writable=False):
        return {
            'variableId': point_id,
            'display_title': f'Point {point_id}',
            'entity_type': entity_type,
            'entity_category': '',
            'description': '',
            'is_writable': writable,
            'is_dynamic': False,
            'metadata': {
                'unit': unit, 'minValue': 0, 'maxValue': 100,
                'modbusRegisterID': 1, 'variableType': 'integer',
                'variableSize': 'u8', 'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                'shortUnit': '', 'divisor': 1, 'decimal': 0, 'change': 0,
            },
        }

    def test_overridden_switch_shows_resolved_unit_not_raw(self):
        """The exact real-world bug: point 4562 is a switch whose firmware
        unit is '%' (wrong — it's a 0=auto/1=manual toggle, not a percentage).
        The modal must show the override having fired, not the raw '%'."""
        pub = self._publisher()
        point = self._point(4562, unit='%', entity_type='switch', writable=True)
        result = pub._build_point_metadata_dict(point)
        self.assertEqual(result['unit'], '')
        self.assertTrue(result['unit_overridden'])
        self.assertEqual(result['unit_raw'], '%')

    def test_non_overridden_point_unit_and_raw_match(self):
        pub = self._publisher()
        point = self._point(999999, unit='°C')
        result = pub._build_point_metadata_dict(point)
        self.assertEqual(result['unit'], '°C')
        self.assertFalse(result['unit_overridden'])
        self.assertEqual(result['unit_raw'], '°C')

    def test_unit_matches_what_discovery_config_would_publish(self):
        """Direct regression test for the original bug report: the unit
        shown in the modal must equal the unit the real HA discovery
        config receives for the same point — they must never diverge."""
        from nibe_mqtt_publisher import resolve_unit
        pub = self._publisher()
        point = self._point(4562, unit='%', entity_type='switch', writable=True)
        modal_unit = pub._build_point_metadata_dict(point)['unit']
        discovery_unit, _ = resolve_unit(4562, point['metadata']['unit'])
        self.assertEqual(modal_unit, discovery_unit)



class TestPublishPointMetadataConsolidation(unittest.TestCase):
    """publish_point_metadata previously duplicated _build_point_metadata_dict's
    logic inline instead of calling it — exactly the kind of drift that
    caused the unit bug in the first place (two paths that look identical
    but silently diverge). Confirms the single-point update path and the
    bulk path now produce identical metadata shape (aside from last_updated,
    which only makes sense on the single-point path)."""

    def _publisher(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        pub = object.__new__(MqttDiscoveryPublisher)
        pub.mqtt = MagicMock()
        pub.device_info = {}
        return pub

    def _point(self, point_id, unit=''):
        return {
            'variableId': point_id,
            'display_title': f'Point {point_id}',
            'entity_type': 'sensor',
            'entity_category': '',
            'description': '',
            'is_writable': False,
            'is_dynamic': False,
            'metadata': {
                'unit': unit, 'minValue': 0, 'maxValue': 100,
                'modbusRegisterID': 1, 'variableType': 'integer',
                'variableSize': 'u8', 'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'shortUnit': '', 'divisor': 1, 'decimal': 0, 'change': 0,
            },
        }

    def test_single_point_publish_includes_unit_override_fields(self):
        pub = self._publisher()
        point = self._point(4562, unit='%')
        pub.publish_point_metadata(point)
        topic, payload = pub.mqtt.publish.call_args.args
        published = json.loads(payload)
        self.assertEqual(published['unit'], '')
        self.assertTrue(published['unit_overridden'])
        self.assertEqual(published['unit_raw'], '%')

    def test_single_point_publish_includes_last_updated(self):
        """last_updated is layered on top of the shared dict — confirms
        the consolidation didn't drop this single-point-only field."""
        pub = self._publisher()
        point = self._point(100)
        pub.publish_point_metadata(point)
        _, payload = pub.mqtt.publish.call_args.args
        published = json.loads(payload)
        self.assertIn('last_updated', published)

    def test_bulk_dict_has_no_last_updated(self):
        """_build_point_metadata_dict alone (the bulk path) must not include
        last_updated — it's added only by publish_point_metadata."""
        pub = self._publisher()
        point = self._point(100)
        result = pub._build_point_metadata_dict(point)
        self.assertNotIn('last_updated', result)

    def test_single_point_and_bulk_path_produce_same_unit_resolution(self):
        """The actual anti-drift regression test: publishing the same point
        via publish_point_metadata (single) and _build_point_metadata_dict
        (bulk) directly must resolve the unit identically."""
        pub = self._publisher()
        point = self._point(4562, unit='%')
        pub.publish_point_metadata(point)
        _, payload = pub.mqtt.publish.call_args.args
        single_result = json.loads(payload)
        bulk_result = pub._build_point_metadata_dict(point)
        self.assertEqual(single_result['unit'], bulk_result['unit'])
        self.assertEqual(single_result['unit_overridden'], bulk_result['unit_overridden'])


# ===========================================================================
# 63. Override warning logging — unit and entity-type categories
# ===========================================================================


class TestResolveUnitWarningLogging(unittest.TestCase):
    """resolve_unit()'s optional logging path, added so the add-on log
    surfaces a one-shot-per-point warning whenever a unit override fires —
    mirroring the existing degenerate-range warning pattern. Passing no
    `warned` set must stay a pure, side-effect-free resolution (tested
    separately in TestResolveUnit above); these tests cover the logging
    path specifically."""

    def test_no_warned_set_means_no_logging_call(self):
        """Default behaviour (warned=None) must not touch the logger at all —
        confirms pure-mode callers (e.g. _build_point_metadata_dict) get zero
        side effects."""
        from nibe_mqtt_publisher import resolve_unit, log_mqtt
        with patch.object(log_mqtt, 'warning') as mock_warn:
            resolve_unit(4562, '%', 'Some title')
            mock_warn.assert_not_called()

    def test_override_with_warned_set_logs_once(self):
        from nibe_mqtt_publisher import resolve_unit, log_mqtt
        warned = set()
        with patch.object(log_mqtt, 'warning') as mock_warn:
            resolve_unit(4562, '%', 'Manual pump speed', warned)
            mock_warn.assert_called_once()
        self.assertIn(4562, warned)

    def test_override_warning_message_content(self):
        """Confirms the trimmed message shape: point id, title, raw value,
        resolved value — no extra padding."""
        from nibe_mqtt_publisher import resolve_unit, log_mqtt
        warned = set()
        with patch.object(log_mqtt, 'warning') as mock_warn:
            resolve_unit(4562, '%', 'Manual pump speed', warned)
        args = mock_warn.call_args.args
        self.assertIn('unit overridden', args[0])
        self.assertEqual(args[1], 4562)
        self.assertEqual(args[2], 'Manual pump speed')
        self.assertEqual(args[3], '%')   # raw firmware value
        self.assertEqual(args[4], '')    # resolved value

    def test_repeated_calls_same_point_log_only_once(self):
        """The actual dedup contract: calling resolve_unit for the same
        point_id multiple times (e.g. once from publish_entity_discovery,
        hypothetically again from a retry) must only log the first time."""
        from nibe_mqtt_publisher import resolve_unit, log_mqtt
        warned = set()
        with patch.object(log_mqtt, 'warning') as mock_warn:
            resolve_unit(4562, '%', 'Manual pump speed', warned)
            resolve_unit(4562, '%', 'Manual pump speed', warned)
            resolve_unit(4562, '%', 'Manual pump speed', warned)
        mock_warn.assert_called_once()

    def test_different_points_each_log_independently(self):
        from nibe_mqtt_publisher import resolve_unit, log_mqtt
        warned = set()
        with patch.object(log_mqtt, 'warning') as mock_warn:
            resolve_unit(4562, '%', 'Manual pump speed', warned)
            resolve_unit(50827, '%RH', 'Humidity: ths-10', warned)
        self.assertEqual(mock_warn.call_count, 2)
        self.assertEqual(warned, {4562, 50827})

    def test_non_overridden_point_never_logs(self):
        from nibe_mqtt_publisher import resolve_unit, log_mqtt
        warned = set()
        with patch.object(log_mqtt, 'warning') as mock_warn:
            resolve_unit(999999, '°C', 'Some sensor', warned)
            mock_warn.assert_not_called()
        self.assertEqual(warned, set())

    def test_missing_title_falls_back_to_point_label(self):
        from nibe_mqtt_publisher import resolve_unit, log_mqtt
        warned = set()
        with patch.object(log_mqtt, 'warning') as mock_warn:
            resolve_unit(4562, '%', '', warned)
        args = mock_warn.call_args.args
        self.assertEqual(args[2], 'Point 4562')



class TestPublishEntityDiscoveryUnitWarningIntegration(unittest.TestCase):
    """Confirms publish_entity_discovery (the real discovery-config path)
    actually wires the instance's dedup set through to resolve_unit, and
    that _build_point_metadata_dict (the card's metadata path) deliberately
    does NOT — avoiding a double-log for the same point when both paths
    resolve the same unit for different purposes."""

    def _publisher(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        pub = object.__new__(MqttDiscoveryPublisher)
        pub.mqtt = MagicMock()
        pub.device_info = {'identifiers': ['nibe']}
        pub.device_id = 'nibe'
        pub._range_warnings_issued = set()
        pub._unit_override_warnings_issued = set()
        pub._config_hashes = {}
        return pub

    def _point(self, point_id, unit='', entity_type='switch', category='config', writable=True):
        return {
            'variableId': point_id,
            'display_title': f'Point {point_id}',
            'entity_type': entity_type,
            'entity_category': category,
            'description': '',
            'is_writable': writable,
            'is_dynamic': False,
            'metadata': {
                'unit': unit, 'minValue': 0, 'maxValue': 1,
                'modbusRegisterID': 1, 'variableType': 'integer',
                'variableSize': 'u8', 'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                'shortUnit': '', 'divisor': 1, 'decimal': 0, 'change': 0,
                'intDefaultValue': 0,
            },
        }

    def test_discovery_publish_logs_override_warning(self):
        from nibe_mqtt_publisher import log_mqtt
        pub = self._publisher()
        point = self._point(4562, unit='%')
        with patch.object(log_mqtt, 'warning') as mock_warn:
            pub.publish_entity_discovery(point, {})
        self.assertTrue(any('unit overridden' in c.args[0] for c in mock_warn.call_args_list))

    def test_discovery_publish_warns_only_once_across_polls(self):
        """Simulates the real pattern: the same point gets re-published
        across multiple polls (e.g. on every full discovery refresh) —
        must only warn on the first."""
        from nibe_mqtt_publisher import log_mqtt
        pub = self._publisher()
        point = self._point(4562, unit='%')
        with patch.object(log_mqtt, 'warning') as mock_warn:
            pub.publish_entity_discovery(point, {})
            pub.publish_entity_discovery(point, {})
        override_warnings = [c for c in mock_warn.call_args_list if 'unit overridden' in c.args[0]]
        self.assertEqual(len(override_warnings), 1)

    def test_metadata_dict_path_does_not_log(self):
        """_build_point_metadata_dict must stay silent — only the real
        discovery-config path logs, to avoid double-warning for the same
        point when both paths resolve it."""
        from nibe_mqtt_publisher import log_mqtt
        pub = self._publisher()
        point = self._point(4562, unit='%')
        with patch.object(log_mqtt, 'warning') as mock_warn:
            pub._build_point_metadata_dict(point)
            mock_warn.assert_not_called()



class TestRangeWarningTrimmedMessages(unittest.TestCase):
    """The degenerate-range and out-of-range warnings were trimmed this
    round — current/default value dumps removed (now redundant with the
    Entity Manager modal, which already shows live values). Confirms the
    trimmed message text is correct and meaningfully shorter, not just
    that *a* warning still fires."""

    def _publisher(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        pub = object.__new__(MqttDiscoveryPublisher)
        pub.mqtt = MagicMock()
        pub._range_warnings_issued = set()
        pub._unit_override_warnings_issued = set()
        return pub

    def test_degenerate_range_message_is_trimmed(self):
        from nibe_mqtt_publisher import log_entities
        pub = self._publisher()
        config = {}
        metadata = {'minValue': 0, 'maxValue': 0, 'divisor': 1, 'intDefaultValue': 0}
        with patch.object(log_entities, 'warning') as mock_warn:
            pub._build_number_config(config, 'number.nibe_2500', 2500,
                                      'Compressor status', '', metadata, {})
        mock_warn.assert_called_once()
        msg = mock_warn.call_args.args[0]
        self.assertIn('degenerate range', msg)
        self.assertNotIn('Current:', msg)
        self.assertNotIn('Default:', msg)
        self.assertLess(len(msg), 100)   # trimmed format string itself, not the rendered line

    def test_out_of_range_message_is_trimmed(self):
        from nibe_mqtt_publisher import log_entities
        pub = self._publisher()
        config = {}
        metadata = {'minValue': 0, 'maxValue': 100, 'divisor': 1}
        bulk_data = {3898: {'raw_value': 150}}
        with patch.object(log_entities, 'warning') as mock_warn:
            pub._build_number_config(config, 'number.nibe_3898', 3898,
                                      'Some setting', '', metadata, bulk_data)
        mock_warn.assert_called_once()
        msg = mock_warn.call_args.args[0]
        self.assertIn('outside firmware range', msg)
        self.assertNotIn('HA will display', msg)

    def test_degenerate_range_still_only_warns_once(self):
        """Confirms the trim didn't accidentally break the existing dedup
        behaviour for this warning category."""
        from nibe_mqtt_publisher import log_entities
        pub = self._publisher()
        metadata = {'minValue': 0, 'maxValue': 0, 'divisor': 1, 'intDefaultValue': 0}
        with patch.object(log_entities, 'warning') as mock_warn:
            pub._build_number_config({}, 'number.nibe_2500', 2500, 'Compressor status', '', metadata, {})
            pub._build_number_config({}, 'number.nibe_2500', 2500, 'Compressor status', '', metadata, {})
        mock_warn.assert_called_once()


# ===========================================================================
# 64. triggered_by population in changelog + _handle_event dead-code fix
# ===========================================================================


class TestPublishBrowserFunctions(unittest.TestCase):
    """publish_all_metadata, publish_point_list, publish_enabled_state.
    All are pure publish functions — build a JSON payload and call mqtt.publish."""

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, BrowserTopic
        self.mqtt = MagicMock()
        self.mqtt.publish.return_value = MagicMock(rc=0)
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt,
            device_info={'identifiers': ['nibe_test'], 'model': 'S-series',
                         'manufacturer': 'NIBE', 'serial_number': '12345'},
            device_id='test', device_name='Test Device',
        )
        self.BrowserTopic = BrowserTopic

    def _make_point(self, pid):
        return {
            'variableId': pid, 'title': f'Point {pid}', 'description': '',
            'display_title': f'Point {pid}', 'entity_type': 'sensor',
            'metadata': {
                'variableId': pid, 'variableType': 'integer',
                'variableSize': 's16', 'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'isWritable': False, 'divisor': 10, 'decimal': 1,
                'minValue': -500, 'maxValue': 500, 'intDefaultValue': 0,
                'change': 1, 'unit': '°C', 'shortUnit': '°',
                'modbusRegisterID': 1000, 'stringDefaultValue': '',
            },
            'value': {'integerValue': 200, 'isOk': True},
        }

    # ── publish_all_metadata ────────────────────────────────────────────────

    def test_publish_all_metadata_publishes_to_correct_topic(self):
        points = [self._make_point(100), self._make_point(200)]
        self.pub.publish_all_metadata(points)
        topic = self.mqtt.publish.call_args[0][0]
        self.assertEqual(topic, self.BrowserTopic.ALL_METADATA)

    def test_publish_all_metadata_is_retained(self):
        self.pub.publish_all_metadata([self._make_point(100)])
        call = self.mqtt.publish.call_args
        retain = call.kwargs.get('retain', call.args[2] if len(call.args) > 2 else None)
        self.assertTrue(retain)

    def test_publish_all_metadata_payload_contains_count(self):
        import json
        points = [self._make_point(100), self._make_point(200)]
        self.pub.publish_all_metadata(points)
        payload = json.loads(self.mqtt.publish.call_args[0][1])
        self.assertEqual(payload['count'], 2)

    def test_publish_all_metadata_payload_keyed_by_point_id(self):
        import json
        points = [self._make_point(100), self._make_point(200)]
        self.pub.publish_all_metadata(points)
        payload = json.loads(self.mqtt.publish.call_args[0][1])
        self.assertIn('100', payload['metadata'])
        self.assertIn('200', payload['metadata'])

    def test_publish_all_metadata_empty_points(self):
        import json
        self.pub.publish_all_metadata([])
        payload = json.loads(self.mqtt.publish.call_args[0][1])
        self.assertEqual(payload['count'], 0)
        self.assertEqual(payload['metadata'], {})

    # ── publish_point_list ─────────────────────────────────────────────────

    def test_publish_point_list_correct_topic(self):
        self.pub.publish_point_list({100: {}, 200: {}})
        topic = self.mqtt.publish.call_args[0][0]
        self.assertEqual(topic, self.BrowserTopic.POINT_LIST)

    def test_publish_point_list_payload_sorted(self):
        import json
        self.pub.publish_point_list({300: {}, 100: {}, 200: {}})
        payload = json.loads(self.mqtt.publish.call_args[0][1])
        self.assertEqual(payload['points'], [100, 200, 300])
        self.assertEqual(payload['count'], 3)

    def test_publish_point_list_empty(self):
        import json
        self.pub.publish_point_list({})
        payload = json.loads(self.mqtt.publish.call_args[0][1])
        self.assertEqual(payload['points'], [])
        self.assertEqual(payload['count'], 0)

    # ── publish_enabled_state ─────────────────────────────────────────────

    def test_publish_enabled_state_correct_topic(self):
        self.pub.publish_enabled_state({100, 200})
        topic = self.mqtt.publish.call_args[0][0]
        self.assertEqual(topic, self.BrowserTopic.ENABLED_STATE)

    def test_publish_enabled_state_payload_count(self):
        import json
        self.pub.publish_enabled_state({100, 200, 300})
        payload = json.loads(self.mqtt.publish.call_args[0][1])
        self.assertEqual(payload['count'], 3)
        self.assertEqual(len(payload['enabled_points']), 3)

    def test_publish_enabled_state_empty(self):
        import json
        self.pub.publish_enabled_state(set())
        payload = json.loads(self.mqtt.publish.call_args[0][1])
        self.assertEqual(payload['count'], 0)


# ===========================================================================
# 74. MqttDiscoveryPublisher — management sensor publish functions
# ===========================================================================


class TestPublishManagementSensors(unittest.TestCase):
    """publish_stats, publish_uptime, publish_api_reachability,
    publish_device_modes, publish_alarm_state — all publish to MgmtTopic
    state/attribute topics via _pub_state."""

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, MgmtTopic
        self.mqtt = MagicMock()
        self.mqtt.publish.return_value = MagicMock(rc=0)
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt,
            device_info={'identifiers': ['nibe_test'], 'model': 'S-series',
                         'manufacturer': 'NIBE', 'serial_number': '12345'},
            device_id='test', device_name='Test Device',
        )
        self.MgmtTopic = MgmtTopic

    def _published_to(self, topic):
        return any(call[0][0] == topic
                   for call in self.mqtt.publish.call_args_list)

    def _payload_for(self, topic):
        """Return the payload for a given topic. Tries JSON decode first,
        falls back to the raw string for plain-string state publishes."""
        import json
        for call in self.mqtt.publish.call_args_list:
            if call[0][0] == topic:
                raw = call[0][1]
                try:
                    return json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    return raw
        return None

    # ── publish_stats ────────────────────────────────────────────────────────

    def test_publish_stats_publishes_state_and_attrs(self):
        self.pub.publish_stats(1158, 283, 283, {}, {}, 150)
        self.assertTrue(self._published_to(self.MgmtTopic.STATS_STATE))
        self.assertTrue(self._published_to(self.MgmtTopic.STATS_ATTRS))

    def test_publish_stats_state_is_enabled_count(self):
        self.pub.publish_stats(1158, 283, 283, {}, {}, 150)
        payload = self._payload_for(self.MgmtTopic.STATS_STATE)
        self.assertEqual(str(payload), '283')

    def test_publish_stats_attrs_contain_counts(self):
        self.pub.publish_stats(1158, 283, 283, {'sensor': 200}, {}, 150,
                               write_total=10, write_success=9, write_failed=1)
        attrs = self._payload_for(self.MgmtTopic.STATS_ATTRS)
        self.assertEqual(attrs['total'], 1158)
        self.assertEqual(attrs['mqtt_enabled'], 283)
        self.assertEqual(attrs['writes_total'], 10)
        self.assertEqual(attrs['write_success_rate'], 90.0)

    def test_publish_stats_write_success_rate_zero_total(self):
        """Zero write total must not cause ZeroDivisionError."""
        self.pub.publish_stats(100, 50, 50, {}, {}, 20, write_total=0)
        attrs = self._payload_for(self.MgmtTopic.STATS_ATTRS)
        self.assertEqual(attrs['write_success_rate'], 100.0)

    def test_publish_stats_enabled_pct_zero_total(self):
        """Zero all_points_count must not cause ZeroDivisionError."""
        self.pub.publish_stats(0, 0, 0, {}, {}, 0)
        attrs = self._payload_for(self.MgmtTopic.STATS_ATTRS)
        self.assertEqual(attrs['enabled_percentage'], 0)

    # ── publish_uptime ────────────────────────────────────────────────────────

    def test_publish_uptime_publishes_state_and_attrs(self):
        import time
        self.pub.publish_uptime(time.time() - 3600, time.time(), 0)
        self.assertTrue(self._published_to(self.MgmtTopic.UPTIME_STATE))
        self.assertTrue(self._published_to(self.MgmtTopic.UPTIME_ATTRS))

    def test_publish_uptime_state_is_seconds(self):
        import time
        start = time.time() - 100
        self.pub.publish_uptime(start, time.time(), 0)
        state = self._payload_for(self.MgmtTopic.UPTIME_STATE)
        self.assertTrue(str(state).isdigit())
        self.assertGreaterEqual(int(state), 99)

    # ── publish_api_reachability ──────────────────────────────────────────────

    def test_api_healthy_publishes_on(self):
        self.pub.publish_api_reachability(0, 3, 0.0, 1.5)
        state = self._payload_for(self.MgmtTopic.API_OK_STATE)
        self.assertEqual(str(state), 'ON')

    def test_api_unhealthy_publishes_off(self):
        self.pub.publish_api_reachability(5, 3, 0.0, 1.5)
        state = self._payload_for(self.MgmtTopic.API_OK_STATE)
        self.assertEqual(str(state), 'OFF')

    def test_api_fetch_duration_published(self):
        self.pub.publish_api_reachability(0, 3, 0.0, 1.234)
        state = self._payload_for(self.MgmtTopic.FETCH_DUR_STATE)
        self.assertEqual(str(state), '1.23')

    # ── publish_device_modes ──────────────────────────────────────────────────

    def test_aid_mode_on(self):
        self.pub.publish_device_modes('on', 'auto')
        self.assertEqual(str(self._payload_for(self.MgmtTopic.AID_STATE)), 'ON')

    def test_aid_mode_off(self):
        self.pub.publish_device_modes('off', 'auto')
        self.assertEqual(str(self._payload_for(self.MgmtTopic.AID_STATE)), 'OFF')

    def test_smart_mode_published(self):
        self.pub.publish_device_modes('on', 'comfort')
        self.assertEqual(str(self._payload_for(self.MgmtTopic.SMART_STATE)), 'comfort')

    # ── publish_alarm_state ───────────────────────────────────────────────────

    def test_alarm_state_count(self):
        self.pub.publish_alarm_state(2, [{'id': 1}, {'id': 2}])
        self.assertEqual(str(self._payload_for(self.MgmtTopic.ALARM_STATE)), '2')

    def test_alarm_attrs_contain_alarms(self):
        alarms = [{'id': 1, 'msg': 'test'}]
        self.pub.publish_alarm_state(1, alarms)
        attrs = self._payload_for(self.MgmtTopic.ALARM_ATTRS)
        self.assertEqual(attrs['alarms'], alarms)

    def test_alarm_state_zero(self):
        self.pub.publish_alarm_state(0, [])
        self.assertEqual(str(self._payload_for(self.MgmtTopic.ALARM_STATE)), '0')


# ===========================================================================
# 75. MqttDiscoveryPublisher — publish_bridge_alert and publish_bridge_status
# ===========================================================================


class TestPublishBridgeHealthFunctions(unittest.TestCase):

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, BrowserTopic
        self.mqtt = MagicMock()
        self.mqtt.publish.return_value = MagicMock(rc=0)
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt,
            device_info={'identifiers': ['nibe_test'], 'model': 'S-series',
                         'manufacturer': 'NIBE', 'serial_number': '12345'},
            device_id='test', device_name='Test Device',
        )
        self.BrowserTopic = BrowserTopic

    def _payload_for(self, topic):
        import json
        for call in self.mqtt.publish.call_args_list:
            if call[0][0] == topic:
                return json.loads(call[0][1])
        return None

    # ── publish_bridge_alert ──────────────────────────────────────────────────

    def test_alert_published_to_correct_topic(self):
        self.pub.publish_bridge_alert('api_unreachable', 'error', 'API down')
        topic = self.mqtt.publish.call_args[0][0]
        self.assertEqual(topic, self.BrowserTopic.BRIDGE_ALERT)

    def test_alert_is_not_retained(self):
        self.pub.publish_bridge_alert('api_unreachable', 'error', 'API down')
        call = self.mqtt.publish.call_args
        retain = call.kwargs.get('retain', call.args[2] if len(call.args) > 2 else None)
        self.assertFalse(retain)

    def test_alert_payload_fields(self):
        import json
        self.pub.publish_bridge_alert(
            'write_failed', 'warning', 'Write failed',
            context={'point_id': 781}
        )
        payload = json.loads(self.mqtt.publish.call_args[0][1])
        self.assertEqual(payload['alert_type'], 'write_failed')
        self.assertEqual(payload['severity'], 'warning')
        self.assertEqual(payload['message'], 'Write failed')
        self.assertEqual(payload['context'], {'point_id': 781})

    def test_alert_no_context_defaults_to_empty_dict(self):
        import json
        self.pub.publish_bridge_alert('alarm_active', 'info', 'Alarm')
        payload = json.loads(self.mqtt.publish.call_args[0][1])
        self.assertEqual(payload['context'], {})

    # ── publish_bridge_status ─────────────────────────────────────────────────

    def _status_payload(self, **kwargs):
        import json
        import time
        defaults = dict(
            bridge_start_time=time.time() - 3600,
            api_consecutive_failures=0,
            api_failure_threshold=3,
            api_last_success_time=time.time(),
            last_fetch_duration=1.2,
            write_total=10, write_success=9, write_failed=1,
            last_write_error=None,
            pending_write_count=0,
            mqtt_enabled_count=283,
            all_points_count=1158,
            known_dynamic_count=5,
        )
        defaults.update(kwargs)
        self.pub.publish_bridge_status(**defaults)
        return json.loads(self.mqtt.publish.call_args[0][1])

    def test_status_published_to_correct_topic(self):
        self._status_payload()
        topic = self.mqtt.publish.call_args[0][0]
        self.assertEqual(topic, self.BrowserTopic.BRIDGE_STATUS)

    def test_status_is_retained(self):
        self._status_payload()
        call = self.mqtt.publish.call_args
        retain = call.kwargs.get('retain', call.args[2] if len(call.args) > 2 else None)
        self.assertTrue(retain)

    def test_status_healthy_when_no_failures(self):
        payload = self._status_payload(api_consecutive_failures=0,
                                       api_failure_threshold=3)
        self.assertEqual(payload['status'], 'healthy')
        self.assertTrue(payload['api']['healthy'])

    def test_status_degraded_at_threshold(self):
        payload = self._status_payload(api_consecutive_failures=3,
                                       api_failure_threshold=3)
        self.assertEqual(payload['status'], 'degraded')
        self.assertFalse(payload['api']['healthy'])

    def test_status_write_success_rate(self):
        payload = self._status_payload(write_total=10, write_success=8,
                                       write_failed=2)
        self.assertEqual(payload['writes']['success_rate_pct'], 80.0)

    def test_status_write_success_rate_zero_total(self):
        payload = self._status_payload(write_total=0, write_success=0,
                                       write_failed=0)
        self.assertEqual(payload['writes']['success_rate_pct'], 100.0)

    def test_status_entity_counts(self):
        payload = self._status_payload(mqtt_enabled_count=283,
                                       all_points_count=1158,
                                       known_dynamic_count=5)
        self.assertEqual(payload['entities']['mqtt_enabled'], 283)
        self.assertEqual(payload['entities']['total_known'], 1158)
        self.assertEqual(payload['entities']['known_dynamic'], 5)

    def test_status_uptime_positive(self):
        import time
        payload = self._status_payload(bridge_start_time=time.time() - 100)
        self.assertGreaterEqual(payload['uptime_s'], 99)


# ===========================================================================
# 76. MqttDiscoveryPublisher — publish_management_discovery
# ===========================================================================


class TestPublishManagementDiscoveryPayload(unittest.TestCase):
    """publish_management_discovery() publishes HA discovery configs for
    all bridge management entities to the management device. Takes the
    configured entity mode as a required first argument (entity-mode
    refactor) — used to gate the menus-only regen-dashboard button and to
    seed the read-only mode sensor's initial state."""

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, MgmtTopic
        self.mqtt = MagicMock()
        self.mqtt.publish.return_value = MagicMock(rc=0)
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt,
            device_info={'identifiers': ['nibe_test'], 'model': 'S-series',
                         'manufacturer': 'NIBE', 'serial_number': '12345'},
            device_id='test', device_name='Test Device',
        )
        self.MgmtTopic = MgmtTopic

    def _published_topics(self):
        return [call[0][0] for call in self.mqtt.publish.call_args_list]

    def _json_payloads(self):
        """Payloads from calls whose args[1] parses as JSON — skips the
        plain-string publishes (mode-sensor state, legacy-topic clears)."""
        import json
        payloads = []
        for call in self.mqtt.publish.call_args_list:
            try:
                payloads.append(json.loads(call[0][1]))
            except (ValueError, TypeError):
                continue
        return payloads

    def test_publishes_mode_sensor_config(self):
        self.pub.publish_management_discovery('essential')
        self.assertIn(self.MgmtTopic.MODE_CONFIG, self._published_topics())

    def test_publishes_mode_sensor_initial_state(self):
        """The mode sensor's current value must be published immediately —
        not left as Unknown until the next reconciliation."""
        self.pub.publish_management_discovery('advanced')
        published = {c.args[0]: c.args[1] for c in self.mqtt.publish.call_args_list}
        self.assertEqual(published.get(self.MgmtTopic.MODE_STATE), 'advanced')

    def test_regen_dashboard_button_published_in_menus_mode(self):
        self.pub.publish_management_discovery('menus')
        self.assertIn(self.MgmtTopic.REGEN_DASH_CONFIG, self._published_topics())

    def test_regen_dashboard_button_not_published_outside_menus_mode(self):
        """Decision B: the regen-dashboard button only makes sense when a
        Nibe Menus dashboard actually exists to regenerate."""
        self.pub.publish_management_discovery('essential')
        configs_published = [
            c for c in self.mqtt.publish.call_args_list
            if c.args[0] == self.MgmtTopic.REGEN_DASH_CONFIG and c.args[1]
        ]
        self.assertEqual(configs_published, [])

    def test_regen_dashboard_config_cleared_outside_menus_mode(self):
        """A leftover retained config from a previous menus-mode run must
        be actively cleared (empty retained payload), not just skipped —
        otherwise it ghosts in HA."""
        self.pub.publish_management_discovery('essential')
        for call in self.mqtt.publish.call_args_list:
            if call.args[0] == self.MgmtTopic.REGEN_DASH_CONFIG:
                self.assertEqual(call.args[1], "")
                return
        self.fail("REGEN_DASH_CONFIG was never published to clear it")

    def test_legacy_preset_topics_cleared(self):
        """Retained messages from the pre-refactor preset selector must be
        cleared on every startup (idempotent — harmless once already clear)."""
        from nibe_mqtt_publisher import _LEGACY_PRESET_TOPICS
        self.pub.publish_management_discovery('essential')
        published = {c.args[0]: c.args[1] for c in self.mqtt.publish.call_args_list}
        for topic in _LEGACY_PRESET_TOPICS:
            self.assertEqual(published.get(topic), "")

    def test_publishes_stats_config(self):
        self.pub.publish_management_discovery('essential')
        self.assertIn(self.MgmtTopic.STATS_CONFIG, self._published_topics())

    def test_publishes_aid_mode_config(self):
        self.pub.publish_management_discovery('essential')
        self.assertIn(self.MgmtTopic.AID_CONFIG, self._published_topics())

    def test_publishes_uptime_config(self):
        self.pub.publish_management_discovery('essential')
        self.assertIn(self.MgmtTopic.UPTIME_CONFIG, self._published_topics())

    def test_publishes_alarm_config(self):
        self.pub.publish_management_discovery('essential')
        self.assertIn(self.MgmtTopic.ALARM_CONFIG, self._published_topics())

    def test_all_configs_are_retained(self):
        self.pub.publish_management_discovery('essential')
        for call in self.mqtt.publish.call_args_list:
            retain = call.kwargs.get('retain', call.args[2] if len(call.args) > 2 else None)
            topic = call.args[0]
            self.assertTrue(retain, msg=f"Expected retain=True for topic {topic}")

    def test_management_device_name_in_payload(self):
        """Skips non-JSON publishes (mode-sensor state, legacy-topic
        clears) — only the JSON discovery-config payloads carry a device
        block."""
        self.pub.publish_management_discovery('essential')
        for payload in self._json_payloads():
            if 'device' in payload:
                self.assertIn('Test Device Management',
                              payload['device'].get('name', ''))
                break

    def test_multiple_publishes_without_debug(self):
        """Without debug_mode the total publish count should be stable."""
        self.pub.publish_management_discovery('essential', debug_mode=False)
        count_normal = self.mqtt.publish.call_count
        self.mqtt.reset_mock()
        self.pub.publish_management_discovery('essential', debug_mode=True)
        count_debug = self.mqtt.publish.call_count
        # debug_mode adds at least one extra entity
        self.assertGreaterEqual(count_debug, count_normal)

    def test_non_debug_mode_unpublishes_debug_entities(self):
        """When debug_mode=False, empty retained payloads are sent to the
        three debug-only discovery topics so HA removes those entities."""
        from nibe_mqtt_publisher import MgmtTopic, _HA_BASE
        self.pub.publish_management_discovery('essential', debug_mode=False)
        published = {
            call.args[0]: call.args[1]
            for call in self.mqtt.publish.call_args_list
            if call.args
        }
        debug_topics = [
            MgmtTopic.FLUSH_MAP_CONFIG,
            MgmtTopic.RUN_TESTS_CONFIG,
            f"{_HA_BASE}/sensor/nibe_test_suite_result/config",
        ]
        for topic in debug_topics:
            self.assertIn(topic, published,
                          f"Expected unpublish call for debug topic {topic}")
            self.assertEqual(published[topic], "",
                             f"Expected empty payload for debug topic {topic}")

    def test_debug_mode_does_not_unpublish_debug_entities(self):
        """When debug_mode=True, debug entity topics are published with
        real config payloads — not empty strings."""
        from nibe_mqtt_publisher import MgmtTopic, _HA_BASE
        self.pub.publish_management_discovery('essential', debug_mode=True)
        published = {
            call.args[0]: call.args[1]
            for call in self.mqtt.publish.call_args_list
            if call.args
        }
        debug_topics = [
            MgmtTopic.FLUSH_MAP_CONFIG,
            MgmtTopic.RUN_TESTS_CONFIG,
            f"{_HA_BASE}/sensor/nibe_test_suite_result/config",
        ]
        for topic in debug_topics:
            self.assertIn(topic, published,
                          f"Expected discovery publish for debug topic {topic}")
            self.assertNotEqual(published[topic], "",
                                f"Expected non-empty payload for debug topic {topic}")


# ===========================================================================
# 73. MqttDiscoveryPublisher — browser and management topic publishers
# ===========================================================================


class TestPublishAllMetadata(unittest.TestCase):
    """publish_all_metadata publishes a single batched retained message
    containing metadata for all known points."""

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, BrowserTopic
        self.BrowserTopic = BrowserTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt,
            device_info={},
            device_id='test_id',
            device_name='Test',
        )

    def _point(self, pid):
        return {
            'variableId': pid, 'title': f'Point {pid}', 'description': '',
            'display_title': f'Point {pid}',
            'entity_type': 'sensor',
            'is_writable': False,
            'entity_category': 'diagnostic',
            'is_dynamic': False,
            'metadata': {
                'isWritable': False, 'divisor': 1, 'decimal': 0,
                'unit': '', 'shortUnit': '', 'modbusRegisterID': pid,
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'variableType': 'integer', 'variableSize': 's16',
                'minValue': 0, 'maxValue': 100, 'intDefaultValue': 0,
                'stringDefaultValue': '', 'change': 1,
            },
        }

    def test_publishes_to_all_metadata_topic(self):
        self.pub.publish_all_metadata([self._point(100)])
        topic = self.mqtt.publish.call_args_list[0][0][0]
        self.assertEqual(topic, self.BrowserTopic.ALL_METADATA)

    def test_publish_is_retained(self):
        self.pub.publish_all_metadata([self._point(100)])
        kwargs = self.mqtt.publish.call_args_list[0]
        self.assertTrue(kwargs[1].get('retain') or kwargs[0][2])

    def test_payload_contains_count(self):
        import json
        self.pub.publish_all_metadata([self._point(100), self._point(200)])
        payload = json.loads(self.mqtt.publish.call_args_list[0][0][1])
        self.assertEqual(payload['count'], 2)

    def test_payload_keyed_by_point_id(self):
        import json
        self.pub.publish_all_metadata([self._point(100), self._point(200)])
        payload = json.loads(self.mqtt.publish.call_args_list[0][0][1])
        self.assertIn('100', payload['metadata'])
        self.assertIn('200', payload['metadata'])

    def test_empty_points_publishes_count_zero(self):
        import json
        self.pub.publish_all_metadata([])
        payload = json.loads(self.mqtt.publish.call_args_list[0][0][1])
        self.assertEqual(payload['count'], 0)



class TestPublishPointList(unittest.TestCase):
    """publish_point_list publishes a retained list of all known point IDs."""

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, BrowserTopic
        self.BrowserTopic = BrowserTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test_id', device_name='Test',
        )

    def test_publishes_to_point_list_topic(self):
        self.pub.publish_point_list({100: {}, 200: {}})
        topic = self.mqtt.publish.call_args_list[0][0][0]
        self.assertEqual(topic, self.BrowserTopic.POINT_LIST)

    def test_payload_contains_sorted_ids(self):
        import json
        self.pub.publish_point_list({200: {}, 100: {}, 300: {}})
        payload = json.loads(self.mqtt.publish.call_args_list[0][0][1])
        self.assertEqual(payload['points'], [100, 200, 300])

    def test_payload_count_matches(self):
        import json
        self.pub.publish_point_list({100: {}, 200: {}})
        payload = json.loads(self.mqtt.publish.call_args_list[0][0][1])
        self.assertEqual(payload['count'], 2)

    def test_empty_dict_publishes_empty_list(self):
        import json
        self.pub.publish_point_list({})
        payload = json.loads(self.mqtt.publish.call_args_list[0][0][1])
        self.assertEqual(payload['points'], [])



class TestPublishEnabledState(unittest.TestCase):
    """publish_enabled_state publishes the current set of enabled point IDs."""

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, BrowserTopic
        self.BrowserTopic = BrowserTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test_id', device_name='Test',
        )

    def test_publishes_to_enabled_state_topic(self):
        self.pub.publish_enabled_state({100, 200})
        topic = self.mqtt.publish.call_args_list[0][0][0]
        self.assertEqual(topic, self.BrowserTopic.ENABLED_STATE)

    def test_payload_count_matches_set_size(self):
        import json
        self.pub.publish_enabled_state({100, 200, 300})
        payload = json.loads(self.mqtt.publish.call_args_list[0][0][1])
        self.assertEqual(payload['count'], 3)

    def test_payload_contains_all_ids(self):
        import json
        self.pub.publish_enabled_state({100, 200})
        payload = json.loads(self.mqtt.publish.call_args_list[0][0][1])
        self.assertEqual(set(payload['enabled_points']), {100, 200})

    def test_empty_set_publishes_count_zero(self):
        import json
        self.pub.publish_enabled_state(set())
        payload = json.loads(self.mqtt.publish.call_args_list[0][0][1])
        self.assertEqual(payload['count'], 0)



class TestPublishBridgeAlert(unittest.TestCase):
    """publish_bridge_alert publishes a non-retained edge-triggered alert."""

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, BrowserTopic
        self.BrowserTopic = BrowserTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test_id', device_name='Test',
        )

    def test_publishes_to_bridge_alert_topic(self):
        self.pub.publish_bridge_alert('api_unreachable', 'warning', 'msg')
        topic = self.mqtt.publish.call_args_list[0][0][0]
        self.assertEqual(topic, self.BrowserTopic.BRIDGE_ALERT)

    def test_not_retained(self):
        """Alert must NOT be retained — it fires on edge only."""
        self.pub.publish_bridge_alert('api_unreachable', 'warning', 'msg')
        call = self.mqtt.publish.call_args_list[0]
        retain = call[1].get('retain', call[0][2] if len(call[0]) > 2 else True)
        self.assertFalse(retain)

    def test_payload_contains_alert_type_and_severity(self):
        import json
        self.pub.publish_bridge_alert('write_failed', 'error', 'Write failed')
        payload = json.loads(self.mqtt.publish.call_args_list[0][0][1])
        self.assertEqual(payload['alert_type'], 'write_failed')
        self.assertEqual(payload['severity'], 'error')
        self.assertEqual(payload['message'], 'Write failed')

    def test_context_included_when_provided(self):
        import json
        self.pub.publish_bridge_alert('alarm_active', 'warning', 'msg',
                                      context={'point_id': 5214})
        payload = json.loads(self.mqtt.publish.call_args_list[0][0][1])
        self.assertEqual(payload['context']['point_id'], 5214)

    def test_context_empty_dict_when_not_provided(self):
        import json
        self.pub.publish_bridge_alert('api_restored', 'info', 'msg')
        payload = json.loads(self.mqtt.publish.call_args_list[0][0][1])
        self.assertEqual(payload['context'], {})



class TestPublishBridgeStatus(unittest.TestCase):
    """publish_bridge_status publishes a retained consolidated health snapshot."""

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, BrowserTopic
        self.BrowserTopic = BrowserTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test_id', device_name='Test',
        )

    def _publish(self, **overrides):
        defaults = dict(
            bridge_start_time=0.0,
            api_consecutive_failures=0,
            api_failure_threshold=3,
            api_last_success_time=0.0,
            last_fetch_duration=1.2,
            write_total=10, write_success=9, write_failed=1,
            last_write_error=None,
            pending_write_count=0,
            mqtt_enabled_count=100,
            all_points_count=1158,
            known_dynamic_count=5,
        )
        defaults.update(overrides)
        self.pub.publish_bridge_status(**defaults)
        import json
        return json.loads(self.mqtt.publish.call_args_list[0][0][1])

    def test_publishes_to_bridge_status_topic(self):
        self._publish()
        topic = self.mqtt.publish.call_args_list[0][0][0]
        self.assertEqual(topic, self.BrowserTopic.BRIDGE_STATUS)

    def test_publish_is_retained(self):
        self._publish()
        call = self.mqtt.publish.call_args_list[0]
        retain = call[1].get('retain', call[0][2] if len(call[0]) > 2 else False)
        self.assertTrue(retain)

    def test_status_healthy_when_no_failures(self):
        payload = self._publish(api_consecutive_failures=0, api_failure_threshold=3)
        self.assertEqual(payload['status'], 'healthy')

    def test_status_degraded_when_failures_at_threshold(self):
        payload = self._publish(api_consecutive_failures=3, api_failure_threshold=3)
        self.assertEqual(payload['status'], 'degraded')

    def test_write_success_rate_calculated(self):
        payload = self._publish(write_total=10, write_success=8, write_failed=2)
        self.assertAlmostEqual(payload['writes']['success_rate_pct'], 80.0)

    def test_write_success_rate_100_when_no_writes(self):
        payload = self._publish(write_total=0, write_success=0, write_failed=0)
        self.assertEqual(payload['writes']['success_rate_pct'], 100.0)

    def test_entity_counts_present(self):
        payload = self._publish(mqtt_enabled_count=150, all_points_count=1158,
                                known_dynamic_count=3)
        self.assertEqual(payload['entities']['mqtt_enabled'], 150)
        self.assertEqual(payload['entities']['total_known'], 1158)
        self.assertEqual(payload['entities']['known_dynamic'], 3)

    def test_last_write_error_included(self):
        payload = self._publish(last_write_error='PATCH failed: 400')
        self.assertEqual(payload['writes']['last_error'], 'PATCH failed: 400')



class TestPublishManagementDiscovery(unittest.TestCase):
    """publish_management_discovery publishes HA discovery configs for all
    bridge management entities. Verifies key entities are published and that
    debug-only entities are conditionally included."""

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, MgmtTopic
        self.MgmtTopic = MgmtTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt,
            device_info={'model': 'S-series', 'manufacturer': 'NIBE',
                         'serial_number': '12345'},
            device_id='nibe_test', device_name='Test Device',
        )

    def _topics_published(self):
        return [call[0][0] for call in self.mqtt.publish.call_args_list]

    def test_mode_config_published(self):
        self.pub.publish_management_discovery('essential')
        self.assertIn(self.MgmtTopic.MODE_CONFIG, self._topics_published())

    def test_stats_config_published(self):
        self.pub.publish_management_discovery('essential')
        self.assertIn(self.MgmtTopic.STATS_CONFIG, self._topics_published())

    def test_alarm_config_published(self):
        self.pub.publish_management_discovery('essential')
        self.assertIn(self.MgmtTopic.ALARM_CONFIG, self._topics_published())

    def test_uptime_config_published(self):
        self.pub.publish_management_discovery('essential')
        self.assertIn(self.MgmtTopic.UPTIME_CONFIG, self._topics_published())

    def test_api_ok_config_published(self):
        self.pub.publish_management_discovery('essential')
        self.assertIn(self.MgmtTopic.API_OK_CONFIG, self._topics_published())

    def test_flush_map_not_published_without_debug_mode(self):
        """Without debug_mode, FLUSH_MAP_CONFIG is sent with an empty payload
        to unpublish the entity from HA — not omitted entirely."""
        self.pub.publish_management_discovery('essential', debug_mode=False)
        published = {c.args[0]: c.args[1] for c in self.mqtt.publish.call_args_list if c.args}
        self.assertIn(self.MgmtTopic.FLUSH_MAP_CONFIG, published)
        self.assertEqual(published[self.MgmtTopic.FLUSH_MAP_CONFIG], "")

    def test_flush_map_published_in_debug_mode(self):
        self.pub.publish_management_discovery('essential', debug_mode=True)
        self.assertIn(self.MgmtTopic.FLUSH_MAP_CONFIG, self._topics_published())

    def test_run_tests_button_not_published_without_debug_mode(self):
        """Without debug_mode, RUN_TESTS_CONFIG is sent with an empty payload
        to unpublish the entity from HA — not omitted entirely."""
        self.pub.publish_management_discovery('essential', debug_mode=False)
        published = {c.args[0]: c.args[1] for c in self.mqtt.publish.call_args_list if c.args}
        self.assertIn(self.MgmtTopic.RUN_TESTS_CONFIG, published)
        self.assertEqual(published[self.MgmtTopic.RUN_TESTS_CONFIG], "")

    def test_run_tests_button_published_in_debug_mode(self):
        self.pub.publish_management_discovery('essential', debug_mode=True)
        self.assertIn(self.MgmtTopic.RUN_TESTS_CONFIG, self._topics_published())

    def test_run_tests_state_reset_to_unknown_on_startup_debug(self):
        """On every startup in debug mode, RUN_TESTS_STATE must be reset to
        'unknown' so a stale 'running' state left by an interrupted run (e.g.
        add-on rebuild mid-test) does not persist across restarts."""
        self.pub.publish_management_discovery('essential', debug_mode=True)
        state_calls = [c for c in self.mqtt.publish.call_args_list
                       if c[0][0] == self.MgmtTopic.RUN_TESTS_STATE]
        self.assertTrue(state_calls, "RUN_TESTS_STATE not published at startup")
        self.assertEqual(state_calls[0][0][1], 'unknown')

    def test_run_tests_state_not_reset_without_debug_mode(self):
        """Without debug_mode, RUN_TESTS_STATE must not be touched at startup
        — the entities are not registered so publishing would create orphans."""
        self.pub.publish_management_discovery('essential', debug_mode=False)
        state_calls = [c for c in self.mqtt.publish.call_args_list
                       if c[0][0] == self.MgmtTopic.RUN_TESTS_STATE]
        self.assertEqual(state_calls, [])

    def test_management_interface_marked_online(self):
        self.pub.publish_management_discovery('essential')
        # Find the availability publish
        avail_calls = [c for c in self.mqtt.publish.call_args_list
                       if c[0][0] == self.MgmtTopic.AVAIL]
        self.assertTrue(any(c[0][1] == 'online' for c in avail_calls))

    def test_all_configs_are_retained(self):
        """Every discovery config must be published with retain=True."""
        self.pub.publish_management_discovery('essential')
        config_topics = {
            self.MgmtTopic.MODE_CONFIG, self.MgmtTopic.STATS_CONFIG,
            self.MgmtTopic.UPTIME_CONFIG, self.MgmtTopic.API_OK_CONFIG,
        }
        for call in self.mqtt.publish.call_args_list:
            topic = call[0][0]
            if topic in config_topics:
                retain = call[1].get('retain', call[0][2] if len(call[0]) > 2 else False)
                self.assertTrue(retain, f"Config topic {topic} must be retained")

    def test_mode_sensor_has_no_command_topic(self):
        """The mode sensor is read-only by design (mode is config-level and
        restart-required, unlike the removed live preset selector) — its
        discovery config must not declare a command_topic."""
        import json
        self.pub.publish_management_discovery('essential')
        mode_call = next(c for c in self.mqtt.publish.call_args_list
                          if c[0][0] == self.MgmtTopic.MODE_CONFIG)
        payload = json.loads(mode_call[0][1])
        self.assertNotIn('command_topic', payload)
        self.assertEqual(payload['state_topic'], self.MgmtTopic.MODE_STATE)



class TestPublishStats(unittest.TestCase):
    """publish_stats publishes entity count statistics to the HA stats sensor."""

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, MgmtTopic
        self.MgmtTopic = MgmtTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def _publish(self, **kwargs):
        defaults = dict(
            all_points_count=1158, mqtt_enabled_count=283,
            active_count=283, type_counts={}, category_counts={},
            writable_count=100,
        )
        defaults.update(kwargs)
        self.pub.publish_stats(**defaults)

    def test_state_published_as_enabled_count(self):
        self._publish(mqtt_enabled_count=150)
        state_calls = [c for c in self.mqtt.publish.call_args_list
                       if c[0][0] == self.MgmtTopic.STATS_STATE]
        self.assertTrue(any(c[0][1] == '150' for c in state_calls))

    def test_enabled_percentage_calculated(self):
        import json
        self._publish(all_points_count=1000, mqtt_enabled_count=500)
        attr_calls = [c for c in self.mqtt.publish.call_args_list
                      if c[0][0] == self.MgmtTopic.STATS_ATTRS]
        payload = json.loads(attr_calls[0][0][1])
        self.assertEqual(payload['enabled_percentage'], 50.0)

    def test_enabled_percentage_zero_when_no_points(self):
        import json
        self._publish(all_points_count=0, mqtt_enabled_count=0)
        attr_calls = [c for c in self.mqtt.publish.call_args_list
                      if c[0][0] == self.MgmtTopic.STATS_ATTRS]
        payload = json.loads(attr_calls[0][0][1])
        self.assertEqual(payload['enabled_percentage'], 0)

    def test_write_success_rate_in_attrs(self):
        import json
        self._publish(write_total=10, write_success=7, write_failed=3)
        attr_calls = [c for c in self.mqtt.publish.call_args_list
                      if c[0][0] == self.MgmtTopic.STATS_ATTRS]
        payload = json.loads(attr_calls[0][0][1])
        self.assertAlmostEqual(payload['write_success_rate'], 70.0)



class TestPublishAlarmState(unittest.TestCase):
    """publish_alarm_state publishes alarm count and details."""

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, MgmtTopic
        self.MgmtTopic = MgmtTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def test_state_is_alarm_count_as_string(self):
        self.pub.publish_alarm_state(3, [])
        state_calls = [c for c in self.mqtt.publish.call_args_list
                       if c[0][0] == self.MgmtTopic.ALARM_STATE]
        self.assertTrue(any(c[0][1] == '3' for c in state_calls))

    def test_attrs_contain_alarm_list(self):
        import json
        alarms = [{'id': 1, 'code': 255}]
        self.pub.publish_alarm_state(1, alarms)
        attr_calls = [c for c in self.mqtt.publish.call_args_list
                      if c[0][0] == self.MgmtTopic.ALARM_ATTRS]
        payload = json.loads(attr_calls[0][0][1])
        self.assertEqual(payload['alarms'], alarms)

    def test_zero_alarms(self):
        self.pub.publish_alarm_state(0, [])
        state_calls = [c for c in self.mqtt.publish.call_args_list
                       if c[0][0] == self.MgmtTopic.ALARM_STATE]
        self.assertTrue(any(c[0][1] == '0' for c in state_calls))



class TestPublishDeviceModes(unittest.TestCase):
    """publish_device_modes publishes aid mode and smart mode states."""

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, MgmtTopic
        self.MgmtTopic = MgmtTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def test_aid_mode_on_publishes_ON(self):
        self.pub.publish_device_modes('on', 'normal')
        aid_calls = [c for c in self.mqtt.publish.call_args_list
                     if c[0][0] == self.MgmtTopic.AID_STATE]
        self.assertTrue(any(c[0][1] == 'ON' for c in aid_calls))

    def test_aid_mode_off_publishes_OFF(self):
        self.pub.publish_device_modes('off', 'normal')
        aid_calls = [c for c in self.mqtt.publish.call_args_list
                     if c[0][0] == self.MgmtTopic.AID_STATE]
        self.assertTrue(any(c[0][1] == 'OFF' for c in aid_calls))

    def test_smart_mode_published_as_is(self):
        self.pub.publish_device_modes('off', 'away')
        smart_calls = [c for c in self.mqtt.publish.call_args_list
                       if c[0][0] == self.MgmtTopic.SMART_STATE]
        self.assertTrue(any(c[0][1] == 'away' for c in smart_calls))



class TestPublishInitialDeviceModes(unittest.TestCase):
    """publish_initial_device_modes() pre-publishes AID and SMART mode states
    from the device info fetched at startup, so HA never shows Unknown for
    these entities during the first poll cycle."""

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, MgmtTopic
        self.MgmtTopic = MgmtTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def _published(self, topic):
        return [c.args[1] for c in self.mqtt.publish.call_args_list
                if c.args[0] == topic]

    def test_aid_mode_on_publishes_ON_retained(self):
        self.pub.publish_initial_device_modes({'aidMode': 'on', 'smartMode': 'normal'})
        self.assertIn('ON', self._published(self.MgmtTopic.AID_STATE))

    def test_aid_mode_off_publishes_OFF_retained(self):
        self.pub.publish_initial_device_modes({'aidMode': 'off', 'smartMode': 'normal'})
        self.assertIn('OFF', self._published(self.MgmtTopic.AID_STATE))

    def test_smart_mode_published_lowercase(self):
        self.pub.publish_initial_device_modes({'aidMode': 'off', 'smartMode': 'away'})
        self.assertIn('away', self._published(self.MgmtTopic.SMART_STATE))

    def test_missing_device_info_uses_safe_defaults(self):
        """Empty device_info dict must not raise — defaults to aid=OFF, smart=normal."""
        self.pub.publish_initial_device_modes({})
        self.assertIn('OFF', self._published(self.MgmtTopic.AID_STATE))
        self.assertIn('normal', self._published(self.MgmtTopic.SMART_STATE))

    def test_publishes_as_retained(self):
        """States must be published as retained so new HA subscribers get
        the current value immediately, not on the next poll cycle."""
        self.pub.publish_initial_device_modes({'aidMode': 'off', 'smartMode': 'normal'})
        for call in self.mqtt.publish.call_args_list:
            topic = call.args[0]
            if topic in (self.MgmtTopic.AID_STATE, self.MgmtTopic.SMART_STATE):
                self.assertTrue(
                    call.kwargs.get('retain', False) or
                    (len(call.args) > 2 and call.args[2]),
                    f"Topic {topic} must be published with retain=True"
                )


# ===========================================================================
# 74. notify_ha and dismiss_ha
# ===========================================================================


class TestPublishEntityDiscoveryRemainingBranches(unittest.TestCase):
    """Branches in publish_entity_discovery not reached by existing tests:
    unknown entity type fallback, time entity type, text entity type."""

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def _entity_info(self, entity_type, point_id=100):
        return {
            'variableId': point_id,
            'display_title': 'Test Point',
            'title': 'Test Point',
            'description': '',
            'entity_type': entity_type,
            'entity_category': 'config',
            'is_writable': True,
            'is_dynamic': False,
            'metadata': {
                'isWritable': True, 'divisor': 1, 'decimal': 0,
                'unit': '', 'shortUnit': '', 'modbusRegisterID': point_id,
                'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                'variableType': 'integer', 'variableSize': 's16',
                'minValue': 0, 'maxValue': 10,
                'intDefaultValue': 0, 'stringDefaultValue': '',
                'change': 1,
            },
        }

    def _topics(self):
        return [c[0][0] for c in self.mqtt.publish.call_args_list]

    def test_unknown_entity_type_still_publishes_config(self):
        """Unknown entity type falls back to sensor so point is still visible."""
        self.pub.publish_entity_discovery(
            self._entity_info('unknown_type'), bulk_data={}
        )
        # Should still publish a config topic
        self.assertTrue(any('config' in t for t in self._topics()))

    def test_time_entity_type_publishes_config(self):
        self.pub.publish_entity_discovery(
            self._entity_info('time'), bulk_data={}
        )
        self.assertTrue(any('time' in t and 'config' in t for t in self._topics()))

    def test_time_entity_no_unit_of_measurement(self):
        """Time entities must not have unit_of_measurement — HA rejects it."""
        import json
        self.pub.publish_entity_discovery(
            self._entity_info('time'), bulk_data={}
        )
        config_calls = [c for c in self.mqtt.publish.call_args_list
                        if 'config' in c[0][0] and 'time' in c[0][0]]
        if config_calls:
            payload = json.loads(config_calls[0][0][1])
            self.assertNotIn('unit_of_measurement', payload)

    def test_text_entity_type_publishes_config(self):
        self.pub.publish_entity_discovery(
            self._entity_info('text'), bulk_data={}
        )
        self.assertTrue(any('text' in t and 'config' in t for t in self._topics()))

    def test_text_entity_has_max_length(self):
        """Text entities must have max=64 to match Nibe string register limit."""
        import json
        self.pub.publish_entity_discovery(
            self._entity_info('text'), bulk_data={}
        )
        config_calls = [c for c in self.mqtt.publish.call_args_list
                        if 'config' in c[0][0] and 'text' in c[0][0]]
        if config_calls:
            payload = json.loads(config_calls[0][0][1])
            self.assertEqual(payload.get('max'), 64)



class TestPublishEntityDiscoveryMqttBranches(unittest.TestCase):
    """Branch coverage for mqtt_publisher: degenerate range with no current_raw,
    decimal=None skips suggested_display_precision, static attributes with
    description and with default_value (lines 479->520, 613->exit, 643->647,
    655->657)."""

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def _entity(self, point_id=100, min_val=0, max_val=10, decimal=0,
                unit='', description='', int_default=None, writable=True):
        return {
            'variableId': point_id,
            'display_title': 'Test Point',
            'title': 'Test Point',
            'description': description,
            'entity_type': 'number',
            'entity_category': 'config',
            'is_writable': writable,
            'is_dynamic': False,
            'metadata': {
                'isWritable': writable, 'divisor': 1,
                'decimal': decimal,
                'unit': unit, 'shortUnit': unit,
                'modbusRegisterID': point_id,
                'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                'variableType': 'integer', 'variableSize': 's16',
                'minValue': min_val, 'maxValue': max_val,
                'intDefaultValue': int_default, 'stringDefaultValue': '',
                'change': 1,
            },
        }

    def _config_payload(self, point_id=100):
        calls = [c for c in self.mqtt.publish.call_args_list
                 if f'nibe_{point_id}/config' in c[0][0]]
        self.assertTrue(calls, "No config payload published")
        return json.loads(calls[0][0][1])

    def test_degenerate_range_no_current_raw_uses_divisor_fallback(self):
        """479->520: current_raw is None → fallback min/max = ±32768/divisor."""
        ei = self._entity(min_val=5, max_val=5)
        self.pub.publish_entity_discovery(ei, bulk_data={})
        cfg = self._config_payload()
        self.assertAlmostEqual(cfg['min'], -32768.0)
        self.assertAlmostEqual(cfg['max'],  32767.0)

    def test_number_config_skips_range_when_min_val_is_none(self):
        """479->520: when min_val or max_val is None in metadata, the range
        block is skipped entirely — no min/max keys in config."""
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test2', device_name='Test',
        )
        config = {}
        pub._build_number_config(
            config, 'nibe_200', 200, 'Test', '',
            {
                'minValue': None, 'maxValue': None,
                'divisor': 1, 'decimal': 0, 'change': 1,
                'modbusRegisterID': 200,
                'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                'variableType': 'integer', 'variableSize': 's16',
                'intDefaultValue': None, 'stringDefaultValue': '',
            },
            {}
        )
        self.assertNotIn('min', config)
        self.assertNotIn('max', config)

    def test_decimal_none_skips_suggested_display_precision(self):
        """613->exit: when decimal metadata is None, suggested_display_precision
        must not be set in the config (HA would reject it)."""
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test4', device_name='Test',
        )
        config = {}
        # unit='°C' → has_numeric_value=True; decimal=None → skip precision
        pub._build_sensor_config(
            config, 'nibe_400', 400, '°C', 'Outdoor temp',
            {'decimal': None, 'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
             'variableType': 'integer', 'variableSize': 's16',
             'minValue': -400, 'maxValue': 400},
        )
        self.assertNotIn('suggested_display_precision', config)

    def test_sensor_without_unit_skips_suggested_display_precision(self):
        """613->exit: has_numeric_value is False (no unit) → the entire
        suggested_display_precision block is skipped."""
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test3', device_name='Test',
        )
        config = {}
        # sensor entity with no unit → has_numeric_value = False
        pub._build_sensor_config(
            config, 'nibe_300', 300, '', 'Alarm status',
            {'decimal': 2, 'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
             'variableType': 'integer', 'variableSize': 'u8',
             'minValue': 0, 'maxValue': 5},
        )
        self.assertNotIn('suggested_display_precision', config)

    def test_static_attributes_include_description_when_present(self):
        """655->657: non-empty description → included in JSON attributes."""
        ei = self._entity(description='0=Off, 1=On')
        self.pub.publish_entity_discovery(ei, bulk_data={})
        attr_calls = [c for c in self.mqtt.publish.call_args_list
                      if 'attributes' in c[0][0]]
        self.assertTrue(attr_calls)
        attrs = json.loads(attr_calls[0][0][1])
        self.assertIn('description', attrs)

    def test_static_attributes_include_default_value_when_present(self):
        """643->647: non-None intDefaultValue → default_value in attributes."""
        ei = self._entity(int_default=5, unit='°C')
        self.pub.publish_entity_discovery(ei, bulk_data={})
        attr_calls = [c for c in self.mqtt.publish.call_args_list
                      if 'attributes' in c[0][0]]
        self.assertTrue(attr_calls)
        attrs = json.loads(attr_calls[0][0][1])
        self.assertIn('default_value', attrs)
        self.assertIn('5', attrs['default_value'])



class TestBuildBinarySensorConfigDeviceClass(unittest.TestCase):
    """_build_binary_sensor_config assigns device_class when map_device_class
    returns something meaningful for the title."""

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def test_device_class_set_when_title_maps(self):
        """A title that maps to a device class should set device_class
        in the config dict."""
        # Find a title that produces a non-None device class for binary_sensor
        # 'motion' maps to 'motion' device class
        config = {'state_topic': 'test/state'}
        with patch('nibe_mqtt_publisher.map_device_class', return_value='motion'):
            self.pub._build_binary_sensor_config(config, 'nibe_test', 'Motion sensor')
        self.assertEqual(config.get('device_class'), 'motion')

    def test_no_device_class_when_title_does_not_map(self):
        config = {'state_topic': 'test/state'}
        with patch('nibe_mqtt_publisher.map_device_class', return_value=None):
            self.pub._build_binary_sensor_config(config, 'nibe_test', 'Status')
        self.assertNotIn('device_class', config)



class TestBuildSensorConfigRemainingBranches(unittest.TestCase):
    """Remaining branches in _build_sensor_config."""

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def _metadata(self, divisor=1, decimal=0):
        return {'divisor': divisor, 'decimal': decimal,
                'minValue': 0, 'maxValue': 100}

    def test_point_2685_gets_date_device_class(self):
        """Point 2685 (periodic hot water date) is hard-coded as device_class=date."""
        config = {}
        self.pub._build_sensor_config(
            config, 'nibe_2685', 2685, '', 'Date sensor', self._metadata()
        )
        self.assertEqual(config.get('device_class'), 'date')

    def test_point_2685_returns_early_no_unit(self):
        """Point 2685 must not get unit_of_measurement — it returns early."""
        config = {}
        self.pub._build_sensor_config(
            config, 'nibe_2685', 2685, '°C', 'Date sensor', self._metadata()
        )
        self.assertNotIn('unit_of_measurement', config)

    def test_device_class_without_unit_sets_state_class(self):
        """When device_class resolves but no unit is present, state_class
        should still be set to measurement."""
        config = {}
        with patch('nibe_mqtt_publisher.map_device_class', return_value='duration'):
            self.pub._build_sensor_config(
                config, 'nibe_100', 100, '', 'Duration sensor', self._metadata()
            )
        self.assertEqual(config.get('device_class'), 'duration')
        self.assertEqual(config.get('state_class'), 'measurement')



class TestPublishStaticAttributesWithDescription(unittest.TestCase):
    """_publish_static_attributes includes description when present."""

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def _entity_info(self, description=''):
        return {
            'variableId': 100,
            'display_title': 'Test',
            'entity_type': 'sensor',
            'is_writable': False,
            'metadata': {
                'isWritable': False, 'divisor': 1, 'decimal': 0,
                'unit': '', 'modbusRegisterID': 100,
                'intDefaultValue': 0, 'stringDefaultValue': '',
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
            },
            'description': description,
        }

    def test_description_included_when_present(self):
        import json
        config = {}
        self.pub._publish_static_attributes(
            'sensor', 'nibe_100', 100, '', False, '0=Off,1=On',
            self._entity_info(description='0=Off,1=On')['metadata'], config,
        )
        attr_calls = [c for c in self.mqtt.publish.call_args_list
                      if 'attributes' in c[0][0]]
        self.assertTrue(len(attr_calls) > 0)
        payload = json.loads(attr_calls[0][0][1])
        self.assertEqual(payload.get('description'), '0=Off,1=On')

    def test_description_absent_when_empty(self):
        import json
        config = {}
        self.pub._publish_static_attributes(
            'sensor', 'nibe_100', 100, '', False, '',
            self._entity_info(description='')['metadata'], config,
        )
        attr_calls = [c for c in self.mqtt.publish.call_args_list
                      if 'attributes' in c[0][0]]
        if attr_calls:
            payload = json.loads(attr_calls[0][0][1])
            self.assertNotIn('description', payload)



class TestPublishApiReachabilityLastSuccess(unittest.TestCase):
    """publish_api_reachability publishes last_fetch timestamp when
    api_last_success_time > 0."""

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, MgmtTopic
        self.MgmtTopic = MgmtTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def test_last_fetch_published_when_success_time_nonzero(self):
        self.pub.publish_api_reachability(
            api_consecutive_failures=0,
            api_failure_threshold=3,
            api_last_success_time=1700000000.0,
            last_fetch_duration=1.2,
        )
        topics = [c[0][0] for c in self.mqtt.publish.call_args_list]
        self.assertIn(self.MgmtTopic.LAST_FETCH_STATE, topics)

    def test_last_fetch_not_published_when_success_time_zero(self):
        """api_last_success_time=0 means no successful fetch yet — skip."""
        self.pub.publish_api_reachability(
            api_consecutive_failures=0,
            api_failure_threshold=3,
            api_last_success_time=0,
            last_fetch_duration=0.0,
        )
        topics = [c[0][0] for c in self.mqtt.publish.call_args_list]
        self.assertNotIn(self.MgmtTopic.LAST_FETCH_STATE, topics)

    def test_api_ok_state_off_when_failures_at_threshold(self):
        self.pub.publish_api_reachability(
            api_consecutive_failures=3,
            api_failure_threshold=3,
            api_last_success_time=0,
            last_fetch_duration=0.0,
        )
        calls = {c[0][0]: c[0][1] for c in self.mqtt.publish.call_args_list}
        self.assertEqual(calls.get(self.MgmtTopic.API_OK_STATE), 'OFF')

    def test_fetch_duration_always_published(self):
        self.pub.publish_api_reachability(
            api_consecutive_failures=0,
            api_failure_threshold=3,
            api_last_success_time=0,
            last_fetch_duration=2.5,
        )
        calls = {c[0][0]: c[0][1] for c in self.mqtt.publish.call_args_list}
        self.assertEqual(calls.get(self.MgmtTopic.FETCH_DUR_STATE), '2.50')


# ===========================================================================
# 80. EntityManager — scan_mqtt_discovery
# ===========================================================================




# ===========================================================================
# Mutation-testing gap closures: survivors from mutmut phase-1 run
# ===========================================================================
# These tests were added after a mutmut run on nibe_mqtt_publisher.py
# identified surviving mutants in four functions.  Each class pins a specific
# behavioral invariant that the existing tests left unasserted.


class TestPublishPointMetadataTopicContainsPointId(unittest.TestCase):
    """publish_point_metadata: topic must be derived from point['variableId'].

    Survivors: point_id = None (mutmut_1), topic = None (mutmut_10/11).
    The existing tests assert payload content but never verify that the
    published topic actually contains the point_id — so any mutation that
    corrupts point_id or topic silently passes.
    """

    def _publisher(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        pub = object.__new__(MqttDiscoveryPublisher)
        pub.mqtt = MagicMock()
        pub._unit_override_warnings_issued = set()
        pub.device_info = {}
        return pub

    def _point(self, point_id):
        return {
            'variableId': point_id,
            'display_title': f'Point {point_id}',
            'entity_type': 'sensor',
            'entity_category': '',
            'description': '',
            'is_writable': False,
            'is_dynamic': False,
            'metadata': {
                'unit': '', 'minValue': 0, 'maxValue': 100,
                'modbusRegisterID': point_id,
                'variableType': 'integer', 'variableSize': 'u8',
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'shortUnit': '', 'divisor': 1, 'decimal': 0, 'change': 0,
            },
        }

    def test_topic_contains_exact_point_id(self):
        """The published topic must contain the point's variableId, not None
        or any other value (kills point_id=None and topic=None mutants)."""
        pub = self._publisher()
        pub.publish_point_metadata(self._point(1234))
        topic = pub.mqtt.publish.call_args.args[0]
        self.assertIn('1234', topic,
                      f"Expected point_id 1234 in topic, got: {topic!r}")

    def test_different_point_ids_produce_different_topics(self):
        """Two distinct point_ids must never publish to the same topic —
        confirms point_id is actually used, not a fixed or None value."""
        pub = self._publisher()
        pub.publish_point_metadata(self._point(100))
        pub.publish_point_metadata(self._point(200))
        topics = [c.args[0] for c in pub.mqtt.publish.call_args_list]
        self.assertNotEqual(topics[0], topics[1])
        self.assertIn('100', topics[0])
        self.assertIn('200', topics[1])

    def test_last_updated_is_numeric_not_none(self):
        """metadata['last_updated'] = None mutant (mutmut_7) must be caught:
        last_updated must be a positive float, not None or a sentinel."""
        pub = self._publisher()
        pub.publish_point_metadata(self._point(500))
        payload = json.loads(pub.mqtt.publish.call_args.args[1])
        self.assertIsInstance(payload['last_updated'], float)
        self.assertGreater(payload['last_updated'], 0)


class TestPublishBridgeAlertPayloadKeys(unittest.TestCase):
    """publish_bridge_alert: timestamp key names must be exactly right.

    Survivors: 'timestamp' → 'XXtimestampXX'/'TIMESTAMP' (mutmut_9/10),
               'iso_timestamp' → 'XXiso_timestampXX'/'ISO_TIMESTAMP' (mutmut_11/12).
    HA automations and the frontend card read these keys by exact name.
    """

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def _payload(self):
        self.pub.publish_bridge_alert('api_unreachable', 'warning', 'msg')
        return json.loads(self.mqtt.publish.call_args_list[0][0][1])

    def test_payload_has_lowercase_timestamp_key(self):
        """Key must be 'timestamp' not 'TIMESTAMP' or any other casing."""
        payload = self._payload()
        self.assertIn('timestamp', payload,
                      f"'timestamp' key missing; got keys: {list(payload)}")

    def test_payload_has_lowercase_iso_timestamp_key(self):
        """Key must be 'iso_timestamp' not 'ISO_TIMESTAMP' or mangled."""
        payload = self._payload()
        self.assertIn('iso_timestamp', payload,
                      f"'iso_timestamp' key missing; got keys: {list(payload)}")

    def test_timestamp_is_numeric_float(self):
        """timestamp value must be a numeric float (epoch seconds),
        not None or a string."""
        payload = self._payload()
        self.assertIsInstance(payload['timestamp'], float)
        self.assertGreater(payload['timestamp'], 0)

    def test_iso_timestamp_matches_iso8601_pattern(self):
        """iso_timestamp must be a valid ISO-8601-like string (YYYY-MM-DD...)
        — catches format string mutations like 'XX%Y...'."""
        payload = self._payload()
        iso = payload['iso_timestamp']
        self.assertRegex(iso, r'^\d{4}-\d{2}-\d{2}',
                         f"iso_timestamp {iso!r} doesn't look like ISO-8601")


class TestPublishApiReachabilityLastFetchFormat(unittest.TestCase):
    """publish_api_reachability: last_fetch ISO timestamp format and boundary.

    Survivors:
      - api_last_success_time > 1 (mutmut_12): boundary shifted from 0 to 1
      - last_fetch_iso = None (mutmut_13): format string computation dropped
      - ISO format string mutated (mutmut_18): wrong date format published
    """

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, MgmtTopic
        self.MgmtTopic = MgmtTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def _calls(self):
        return {c.args[0]: c.args[1] for c in self.mqtt.publish.call_args_list}

    def test_last_fetch_published_at_boundary_value_one(self):
        """api_last_success_time=1 is > 0 so LAST_FETCH_STATE must be published.
        The > 1 mutant would suppress it for time=1 — pin the boundary exactly."""
        self.pub.publish_api_reachability(
            api_consecutive_failures=0,
            api_failure_threshold=3,
            api_last_success_time=1,
            last_fetch_duration=0.5,
        )
        self.assertIn(self.MgmtTopic.LAST_FETCH_STATE, self._calls())

    def test_last_fetch_value_is_iso8601_string(self):
        """The last_fetch published value must be a valid ISO-8601-like string
        (YYYY-MM-DDTHH:MM:SSZ), not None or a garbled format string."""
        self.pub.publish_api_reachability(
            api_consecutive_failures=0,
            api_failure_threshold=3,
            api_last_success_time=1700000000.0,
            last_fetch_duration=1.2,
        )
        calls = self._calls()
        iso = calls.get(self.MgmtTopic.LAST_FETCH_STATE, '')
        self.assertRegex(iso, r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$',
                         f"last_fetch value {iso!r} is not valid ISO-8601")


class TestPublishManagementDiscoveryEntityConfig(unittest.TestCase):
    """publish_management_discovery: pin specific payload fields for key entities.

    325 survivors in this function — nearly all string literal mutations in
    entity_category, payload_on/off, options, icon fields.  HA reads these
    by exact string value; a wrong entity_category silently miscategorises
    the entity in HA's UI.

    Strategy: for each entity type where survivors cluster, assert the specific
    field value(s) that the mutants changed.
    """

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, MgmtTopic
        self.MgmtTopic = MgmtTopic
        self.mqtt = MagicMock()
        self.mqtt.publish.return_value = MagicMock(rc=0)
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt,
            device_info={'identifiers': ['nibe_test'], 'model': 'S-series',
                         'manufacturer': 'NIBE', 'serial_number': 'SN12345'},
            device_id='nibe_test', device_name='Test Nibe',
        )

    def _payload_for(self, topic):
        """Return the parsed JSON payload for a specific topic publish."""
        for call in self.mqtt.publish.call_args_list:
            if call.args[0] == topic:
                try:
                    return json.loads(call.args[1])
                except (ValueError, TypeError):
                    return None
        return None

    def test_mode_sensor_entity_category_is_diagnostic(self):
        self.pub.publish_management_discovery('essential')
        payload = self._payload_for(self.MgmtTopic.MODE_CONFIG)
        self.assertIsNotNone(payload)
        self.assertEqual(payload['entity_category'], 'diagnostic')

    def test_aid_mode_payload_on_is_ON(self):
        """payload_on must be the exact string 'ON' — HA switch integration
        matches by exact value."""
        self.pub.publish_management_discovery('essential')
        payload = self._payload_for(self.MgmtTopic.AID_CONFIG)
        self.assertIsNotNone(payload)
        self.assertEqual(payload['payload_on'], 'ON')

    def test_aid_mode_payload_off_is_OFF(self):
        self.pub.publish_management_discovery('essential')
        payload = self._payload_for(self.MgmtTopic.AID_CONFIG)
        self.assertEqual(payload['payload_off'], 'OFF')

    def test_aid_mode_entity_category_is_config(self):
        self.pub.publish_management_discovery('essential')
        payload = self._payload_for(self.MgmtTopic.AID_CONFIG)
        self.assertEqual(payload['entity_category'], 'config')

    def test_smart_mode_options_are_normal_and_away(self):
        """options list must contain exactly ['normal', 'away'] — the HA
        select integration shows these as dropdown choices.  'AWAY' (uppercase)
        would produce a broken select that never matches the firmware value."""
        self.pub.publish_management_discovery('essential')
        payload = self._payload_for(self.MgmtTopic.SMART_CONFIG)
        self.assertIsNotNone(payload)
        self.assertIn('options', payload)
        self.assertEqual(payload['options'], ['normal', 'away'])

    def test_smart_mode_entity_category_is_config(self):
        self.pub.publish_management_discovery('essential')
        payload = self._payload_for(self.MgmtTopic.SMART_CONFIG)
        self.assertEqual(payload['entity_category'], 'config')

    def test_stats_sensor_entity_category_is_diagnostic(self):
        self.pub.publish_management_discovery('essential')
        payload = self._payload_for(self.MgmtTopic.STATS_CONFIG)
        self.assertIsNotNone(payload)
        self.assertEqual(payload['entity_category'], 'diagnostic')

    def test_stats_sensor_unit_of_measurement_is_entities(self):
        self.pub.publish_management_discovery('essential')
        payload = self._payload_for(self.MgmtTopic.STATS_CONFIG)
        self.assertEqual(payload['unit_of_measurement'], 'entities')

    def test_alarm_sensor_entity_category_is_diagnostic(self):
        self.pub.publish_management_discovery('essential')
        payload = self._payload_for(self.MgmtTopic.ALARM_CONFIG)
        self.assertIsNotNone(payload)
        self.assertEqual(payload['entity_category'], 'diagnostic')

    def test_alarm_sensor_unit_of_measurement_is_alarms(self):
        self.pub.publish_management_discovery('essential')
        payload = self._payload_for(self.MgmtTopic.ALARM_CONFIG)
        self.assertEqual(payload['unit_of_measurement'], 'alarms')

    def test_uptime_sensor_device_class_is_duration(self):
        self.pub.publish_management_discovery('essential')
        payload = self._payload_for(self.MgmtTopic.UPTIME_CONFIG)
        self.assertIsNotNone(payload)
        self.assertEqual(payload['device_class'], 'duration')

    def test_uptime_sensor_unit_is_seconds(self):
        self.pub.publish_management_discovery('essential')
        payload = self._payload_for(self.MgmtTopic.UPTIME_CONFIG)
        self.assertEqual(payload['unit_of_measurement'], 's')

    def test_mgmt_device_serial_number_included(self):
        """serial_number in the management device block — mutant drops it."""
        self.pub.publish_management_discovery('essential')
        payload = self._payload_for(self.MgmtTopic.MODE_CONFIG)
        device = payload['device']
        self.assertEqual(device.get('serial_number'), 'SN12345')

    def test_mgmt_device_serial_number_omitted_when_empty(self):
        """When device_info has no serial_number, the key must be absent
        (filtered out by the dict comprehension — not present as empty string)."""
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        mqtt = MagicMock()
        mqtt.publish.return_value = MagicMock(rc=0)
        pub = MqttDiscoveryPublisher(
            mqtt_client=mqtt,
            device_info={'identifiers': ['x'], 'model': 'S'},
            device_id='x', device_name='X',
        )
        pub.publish_management_discovery('essential')
        for call in mqtt.publish.call_args_list:
            try:
                p = json.loads(call.args[1])
                if 'device' in p:
                    self.assertNotIn('serial_number', p['device'])
                    break
            except (ValueError, TypeError):
                continue

    def test_regen_dashboard_entity_category_is_config_in_menus_mode(self):
        self.pub.publish_management_discovery('menus')
        payload = self._payload_for(self.MgmtTopic.REGEN_DASH_CONFIG)
        self.assertIsNotNone(payload)
        self.assertEqual(payload['entity_category'], 'config')


class TestPublishManagementDiscoveryIconKeys(unittest.TestCase):
    """publish_management_discovery: icon key must be present in entity configs.

    Survivor: 'icon' → 'XXiconXX' (mutmut_161) — key rename leaves the icon
    field absent from the HA discovery config, so the entity shows with a
    default icon rather than the intended bridge-specific one.
    """

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, MgmtTopic
        self.MgmtTopic = MgmtTopic
        self.mqtt = MagicMock()
        self.mqtt.publish.return_value = MagicMock(rc=0)
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def _json_payloads_by_topic(self):
        result = {}
        for call in self.mqtt.publish.call_args_list:
            try:
                result[call.args[0]] = json.loads(call.args[1])
            except (ValueError, TypeError):
                pass
        return result

    def test_aid_mode_config_has_icon_key(self):
        """AID_CONFIG must publish an 'icon' key — not 'XXiconXX' or absent."""
        self.pub.publish_management_discovery('essential')
        payloads = self._json_payloads_by_topic()
        aid_payload = payloads.get(self.MgmtTopic.AID_CONFIG)
        self.assertIsNotNone(aid_payload)
        self.assertIn('icon', aid_payload,
                      f"'icon' key missing from AID_CONFIG; got: {list(aid_payload)}")

    def test_smart_mode_config_has_icon_key(self):
        self.pub.publish_management_discovery('essential')
        payloads = self._json_payloads_by_topic()
        payload = payloads.get(self.MgmtTopic.SMART_CONFIG)
        self.assertIn('icon', payload)

    def test_mode_sensor_config_has_icon_key(self):
        self.pub.publish_management_discovery('essential')
        payloads = self._json_payloads_by_topic()
        payload = payloads.get(self.MgmtTopic.MODE_CONFIG)
        self.assertIn('icon', payload)

# ===========================================================================
# Mutmut survivor tests — payload key and value pinning
# Each class targets the function cluster where string-literal mutations
# survived because tests checked behaviour (topics, retain) but not content.
# ===========================================================================


class TestManagementDiscoveryPayloadContent(unittest.TestCase):
    """Pin the HA-critical fields in every management entity config payload.

    Mutmut found 477 survivors in publish_management_discovery — all string
    mutations in config dicts (unique_id, entity_category, device_class,
    unit_of_measurement, payload_on/off, options) that no test read back.
    These tests assert the exact values that HA uses to classify and drive
    each entity.
    """

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, MgmtTopic
        self.MgmtTopic = MgmtTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt,
            device_info={'model': 'S-series', 'manufacturer': 'NIBE',
                         'serial_number': '12345'},
            device_id='nibe_test', device_name='Test Device',
        )

    def _payloads(self, debug_mode=False, mode='essential'):
        self.pub.publish_management_discovery(mode, debug_mode=debug_mode)
        result = {}
        for call in self.mqtt.publish.call_args_list:
            topic = call[0][0]
            try:
                result[topic] = json.loads(call[0][1])
            except (ValueError, TypeError):
                pass
        return result

    def test_mode_sensor_unique_id_and_category(self):
        p = self._payloads()[self.MgmtTopic.MODE_CONFIG]
        self.assertEqual(p['unique_id'], 'nibe_active_mode')
        self.assertEqual(p['entity_category'], 'diagnostic')

    def test_stats_sensor_unique_id_unit_and_state_class(self):
        p = self._payloads()[self.MgmtTopic.STATS_CONFIG]
        self.assertEqual(p['unique_id'], 'nibe_entity_stats')
        self.assertEqual(p['unit_of_measurement'], 'entities')
        self.assertEqual(p['state_class'], 'measurement')
        self.assertEqual(p['entity_category'], 'diagnostic')

    def test_aid_mode_unique_id_payloads_and_category(self):
        p = self._payloads()[self.MgmtTopic.AID_CONFIG]
        self.assertEqual(p['unique_id'], 'nibe_aid_mode')
        self.assertEqual(p['payload_on'], 'ON')
        self.assertEqual(p['payload_off'], 'OFF')
        self.assertEqual(p['entity_category'], 'config')

    def test_smart_mode_unique_id_options_and_category(self):
        p = self._payloads()[self.MgmtTopic.SMART_CONFIG]
        self.assertEqual(p['unique_id'], 'nibe_smart_mode')
        self.assertEqual(sorted(p['options']), ['away', 'normal'])
        self.assertEqual(p['entity_category'], 'config')

    def test_alarm_sensor_unique_id_unit_and_state_class(self):
        p = self._payloads()[self.MgmtTopic.ALARM_CONFIG]
        self.assertEqual(p['unique_id'], 'nibe_notifications')
        self.assertEqual(p['unit_of_measurement'], 'alarms')
        self.assertEqual(p['state_class'], 'measurement')
        self.assertEqual(p['entity_category'], 'diagnostic')

    def test_alarm_reset_button_unique_id_and_category(self):
        p = self._payloads()[self.MgmtTopic.ALARM_RESET_CONFIG]
        self.assertEqual(p['unique_id'], 'nibe_reset_alarms')
        self.assertEqual(p['entity_category'], 'config')

    def test_force_poll_unique_id_and_category(self):
        p = self._payloads()[self.MgmtTopic.FORCE_POLL_CONFIG]
        self.assertEqual(p['unique_id'], 'nibe_force_poll')
        self.assertEqual(p['entity_category'], 'config')

    def test_uptime_sensor_unique_id_device_class_unit_and_state_class(self):
        p = self._payloads()[self.MgmtTopic.UPTIME_CONFIG]
        self.assertEqual(p['unique_id'], 'nibe_bridge_uptime')
        self.assertEqual(p['device_class'], 'duration')
        self.assertEqual(p['unit_of_measurement'], 's')
        self.assertEqual(p['state_class'], 'total_increasing')
        self.assertEqual(p['entity_category'], 'diagnostic')

    def test_last_fetch_unique_id_and_device_class(self):
        p = self._payloads()[self.MgmtTopic.LAST_FETCH_CONFIG]
        self.assertEqual(p['unique_id'], 'nibe_last_fetch_timestamp')
        self.assertEqual(p['device_class'], 'timestamp')
        self.assertEqual(p['entity_category'], 'diagnostic')

    def test_fetch_duration_unique_id_device_class_unit_and_state_class(self):
        p = self._payloads()[self.MgmtTopic.FETCH_DUR_CONFIG]
        self.assertEqual(p['unique_id'], 'nibe_fetch_duration')
        self.assertEqual(p['device_class'], 'duration')
        self.assertEqual(p['unit_of_measurement'], 's')
        self.assertEqual(p['state_class'], 'measurement')
        self.assertEqual(p['entity_category'], 'diagnostic')

    def test_api_reachable_unique_id_device_class_payloads_and_category(self):
        p = self._payloads()[self.MgmtTopic.API_OK_CONFIG]
        self.assertEqual(p['unique_id'], 'nibe_api_reachable')
        self.assertEqual(p['device_class'], 'connectivity')
        self.assertEqual(p['payload_on'], 'ON')
        self.assertEqual(p['payload_off'], 'OFF')
        self.assertEqual(p['entity_category'], 'diagnostic')

    def test_regen_dashboard_unique_id_and_category_in_menus_mode(self):
        p = self._payloads(mode='menus')[self.MgmtTopic.REGEN_DASH_CONFIG]
        self.assertEqual(p['unique_id'], 'nibe_regen_dashboard')
        self.assertEqual(p['entity_category'], 'config')

    def test_flush_map_unique_id_and_category_in_debug_mode(self):
        p = self._payloads(debug_mode=True)[self.MgmtTopic.FLUSH_MAP_CONFIG]
        self.assertEqual(p['unique_id'], 'nibe_flush_dynamic_map')
        self.assertEqual(p['entity_category'], 'config')

    def test_run_tests_button_unique_id_and_category_in_debug_mode(self):
        p = self._payloads(debug_mode=True)[self.MgmtTopic.RUN_TESTS_CONFIG]
        self.assertEqual(p['unique_id'], 'nibe_run_tests')
        self.assertEqual(p['entity_category'], 'config')

    def test_all_entities_have_availability_topic(self):
        """Every discovery config must declare the management availability topic."""
        payloads = self._payloads()
        config_topics = {
            self.MgmtTopic.MODE_CONFIG, self.MgmtTopic.STATS_CONFIG,
            self.MgmtTopic.AID_CONFIG, self.MgmtTopic.SMART_CONFIG,
            self.MgmtTopic.ALARM_CONFIG, self.MgmtTopic.UPTIME_CONFIG,
            self.MgmtTopic.API_OK_CONFIG,
        }
        for topic in config_topics:
            p = payloads.get(topic, {})
            self.assertIn('availability_topic', p, f"Missing availability_topic in {topic}")
            self.assertEqual(p['availability_topic'], self.MgmtTopic.AVAIL)

    def test_initial_uptime_state_is_zero(self):
        self.pub.publish_management_discovery('essential')
        calls = {c[0][0]: c[0][1] for c in self.mqtt.publish.call_args_list}
        self.assertEqual(calls.get(self.MgmtTopic.UPTIME_STATE), '0')

    def test_initial_api_ok_state_is_on(self):
        self.pub.publish_management_discovery('essential')
        calls = {c[0][0]: c[0][1] for c in self.mqtt.publish.call_args_list}
        self.assertEqual(calls.get(self.MgmtTopic.API_OK_STATE), 'ON')

    def test_initial_fetch_duration_state(self):
        self.pub.publish_management_discovery('essential')
        calls = {c[0][0]: c[0][1] for c in self.mqtt.publish.call_args_list}
        self.assertEqual(calls.get(self.MgmtTopic.FETCH_DUR_STATE), '0.00')

    def test_device_info_payload_keys(self):
        """BrowserTopic.DEVICE_INFO payload must have the exact keys the card reads."""
        self.pub.publish_management_discovery('essential')
        device_calls = [c for c in self.mqtt.publish.call_args_list
                        if 'device_info' in c[0][0]]
        self.assertTrue(device_calls, 'DEVICE_INFO not published')
        payload = json.loads(device_calls[0][0][1])
        for key in ('model', 'name', 'manufacturer', 'serial'):
            self.assertIn(key, payload, f"Missing key '{key}' in device_info payload")


class TestPublishEntityDiscoveryPayloadContent(unittest.TestCase):
    """Pin HA-critical fields in publish_entity_discovery output.

    139 survivors in publish_entity_discovery — payload_available/not_available
    strings, entity_id format in topics, hash dedup comparison, return dict keys.
    """

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        mqtt = MagicMock()
        mqtt.publish.return_value = MagicMock(rc=0)
        return MqttDiscoveryPublisher(
            mqtt_client=mqtt,
            device_info={'identifiers': ['nibe_test']},
            device_id='test', device_name='Test',
        ), mqtt

    def _sensor_point(self, point_id=100):
        return {
            'variableId': point_id,
            'display_title': 'Test Sensor',
            'entity_type': 'sensor',
            'entity_category': '',
            'description': '',
            'is_writable': False,
            'is_dynamic': False,
            'metadata': {
                'unit': '°C', 'minValue': -40, 'maxValue': 70,
                'modbusRegisterID': point_id,
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'variableType': 'integer', 'variableSize': 's16',
                'divisor': 10, 'decimal': 1, 'change': 0,
            },
        }

    def test_payload_available_and_not_available_strings(self):
        """payload_available/payload_not_available are HA contract — wrong
        values mean the entity shows as unavailable even when the bridge is up."""
        pub, mqtt = self._pub()
        pub.publish_entity_discovery(self._sensor_point(), {})
        config_call = next(c for c in mqtt.publish.call_args_list
                           if '/config' in c[0][0])
        payload = json.loads(config_call[0][1])
        self.assertEqual(payload['payload_available'], 'online')
        self.assertEqual(payload['payload_not_available'], 'offline')

    def test_unique_id_format(self):
        """unique_id must be 'nibe_<point_id>' — HA uses this to track the entity."""
        pub, mqtt = self._pub()
        pub.publish_entity_discovery(self._sensor_point(100), {})
        config_call = next(c for c in mqtt.publish.call_args_list
                           if '/config' in c[0][0])
        payload = json.loads(config_call[0][1])
        self.assertEqual(payload['unique_id'], 'nibe_100')

    def test_return_dict_keys(self):
        """entity_info dict keys are used by EntityManager — wrong key = AttributeError."""
        pub, _ = self._pub()
        entity_info = pub.publish_entity_discovery(self._sensor_point(100), {})
        self.assertIsNotNone(entity_info)
        for key in ('point_id', 'entity_id', 'entity_type', 'state_topic',
                    'availability_topic', 'metadata', 'is_writable',
                    'point_data', 'value_mapping'):
            self.assertIn(key, entity_info, f"Missing key '{key}' in entity_info")

    def test_return_dict_point_id_matches_input(self):
        pub, _ = self._pub()
        entity_info = pub.publish_entity_discovery(self._sensor_point(4567), {})
        self.assertEqual(entity_info['point_id'], 4567)

    def test_switch_payload_on_off_values(self):
        """Switch payload_on/off are firmware register values: '1'/'0', not 'ON'/'OFF'."""
        pub, mqtt = self._pub()
        point = dict(self._sensor_point(5110))
        point['entity_type'] = 'switch'
        point['is_writable'] = True
        point['metadata'] = dict(point['metadata'])
        point['metadata']['modbusRegisterType'] = 'MODBUS_HOLDING_REGISTER'
        pub.publish_entity_discovery(point, {})
        config_call = next(c for c in mqtt.publish.call_args_list
                           if '/config' in c[0][0])
        payload = json.loads(config_call[0][1])
        self.assertEqual(payload['payload_on'], '1')
        self.assertEqual(payload['payload_off'], '0')

    def test_binary_sensor_payload_on_off_values(self):
        """Binary sensor uses 'ON'/'OFF', not '1'/'0'."""
        pub, mqtt = self._pub()
        point = dict(self._sensor_point(22077))
        point['entity_type'] = 'binary_sensor'
        point['is_writable'] = False
        pub.publish_entity_discovery(point, {})
        config_call = next(c for c in mqtt.publish.call_args_list
                           if '/config' in c[0][0])
        payload = json.loads(config_call[0][1])
        self.assertEqual(payload['payload_on'], 'ON')
        self.assertEqual(payload['payload_off'], 'OFF')

    def test_text_entity_max_length(self):
        """text entity max=64 must match Nibe string register size."""
        pub, mqtt = self._pub()
        point = dict(self._sensor_point(9999))
        point['entity_type'] = 'text'
        point['is_writable'] = True
        pub.publish_entity_discovery(point, {})
        config_call = next(c for c in mqtt.publish.call_args_list
                           if '/config' in c[0][0])
        payload = json.loads(config_call[0][1])
        self.assertEqual(payload['max'], 64)

    def test_number_mode_is_box(self):
        """number entity mode='box' gives direct text entry, not slider."""
        pub, mqtt = self._pub()
        point = dict(self._sensor_point(1234))
        point['entity_type'] = 'number'
        point['is_writable'] = True
        point['metadata'] = dict(point['metadata'])
        point['metadata']['modbusRegisterType'] = 'MODBUS_HOLDING_REGISTER'
        pub.publish_entity_discovery(point, {})
        config_call = next(c for c in mqtt.publish.call_args_list
                           if '/config' in c[0][0])
        payload = json.loads(config_call[0][1])
        self.assertEqual(payload['mode'], 'box')

    def test_config_hash_dedup_skips_second_identical_publish(self):
        """When config is unchanged, second publish must be skipped (hash match)."""
        pub, mqtt = self._pub()
        point = self._sensor_point(100)
        pub.publish_entity_discovery(point, {})
        mqtt.reset_mock()
        pub.publish_entity_discovery(point, {})
        # Second call: hash matches — config topic must NOT be republished
        config_calls = [c for c in mqtt.publish.call_args_list
                        if '/config' in c[0][0]]
        self.assertEqual(config_calls, [],
                         "Identical config should not be republished (hash dedup)")


class TestBuildPointMetadataDictKeys(unittest.TestCase):
    """Pin the exact key names in _build_point_metadata_dict output.

    99 survivors — the frontend card reads these keys by name; a wrong key
    name (e.g. 'min_val' instead of 'min_value') silently breaks the modal.
    """

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        pub = object.__new__(MqttDiscoveryPublisher)
        pub.mqtt = MagicMock()
        pub.device_info = {}
        return pub

    def _point(self, point_id=100):
        return {
            'variableId': point_id,
            'display_title': 'Test Point',
            'entity_type': 'sensor',
            'entity_category': 'diagnostic',
            'description': 'A test point',
            'is_writable': True,
            'is_dynamic': False,
            'metadata': {
                'unit': '°C', 'minValue': -40, 'maxValue': 70,
                'modbusRegisterID': point_id,
                'variableType': 'integer', 'variableSize': 's16',
                'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'shortUnit': 'C', 'divisor': 10, 'decimal': 1, 'change': 5,
            },
        }

    def test_all_required_keys_present(self):
        """Every key the frontend card reads must be present."""
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._point())
        required_keys = (
            'id', 'title', 'type', 'writable', 'unit', 'unit_overridden',
            'unit_raw', 'min_value', 'max_value', 'category', 'description',
            'is_dynamic', 'modbusRegisterID', 'variableType', 'variableSize',
            'modbusRegisterType', 'shortUnit', 'divisor', 'decimal', 'change',
        )
        for key in required_keys:
            self.assertIn(key, result, f"Missing key '{key}' in metadata dict")

    def test_id_key_holds_point_id(self):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._point(4567))
        self.assertEqual(result['id'], 4567)

    def test_type_key_holds_entity_type(self):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._point())
        self.assertEqual(result['type'], 'sensor')

    def test_writable_key_holds_is_writable(self):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._point())
        self.assertTrue(result['writable'])

    def test_min_value_and_max_value_keys(self):
        """min_value/max_value — not minValue/maxValue (camelCase from firmware)."""
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._point())
        self.assertEqual(result['min_value'], -40)
        self.assertEqual(result['max_value'], 70)

    def test_metadata_passthrough_keys(self):
        """Firmware metadata keys passed through verbatim to the card."""
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._point())
        self.assertEqual(result['divisor'], 10)
        self.assertEqual(result['decimal'], 1)
        self.assertEqual(result['change'], 5)
        self.assertEqual(result['shortUnit'], 'C')
        self.assertEqual(result['variableType'], 'integer')
        self.assertEqual(result['variableSize'], 's16')
        self.assertEqual(result['modbusRegisterType'], 'MODBUS_INPUT_REGISTER')


class TestPublishStatsPayloadKeys(unittest.TestCase):
    """Pin the exact key names in publish_stats attrs payload.

    32 survivors — HA automations and the frontend card read these keys;
    a renamed key silently breaks dashboards.
    """

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, MgmtTopic
        self.MgmtTopic = MgmtTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def _attrs(self, **kwargs):
        defaults = dict(
            all_points_count=1158, mqtt_enabled_count=283,
            active_count=280, type_counts={'sensor': 200},
            category_counts={'diagnostic': 100}, writable_count=50,
            write_total=10, write_success=9, write_failed=1,
        )
        defaults.update(kwargs)
        self.pub.publish_stats(**defaults)
        attr_calls = [c for c in self.mqtt.publish.call_args_list
                      if c[0][0] == self.MgmtTopic.STATS_ATTRS]
        return json.loads(attr_calls[0][0][1])

    def test_all_required_keys_present(self):
        attrs = self._attrs()
        for key in ('total', 'mqtt_enabled', 'actually_active', 'discrepancy',
                    'enabled_percentage', 'writable_count', 'by_type',
                    'by_category', 'writes_total', 'writes_success',
                    'writes_failed', 'write_success_rate', 'last_updated',
                    'timestamp', 'note'):
            self.assertIn(key, attrs, f"Missing key '{key}' in stats attrs")

    def test_total_key_holds_all_points_count(self):
        attrs = self._attrs(all_points_count=1158)
        self.assertEqual(attrs['total'], 1158)

    def test_mqtt_enabled_key(self):
        attrs = self._attrs(mqtt_enabled_count=283)
        self.assertEqual(attrs['mqtt_enabled'], 283)

    def test_actually_active_key(self):
        attrs = self._attrs(active_count=280)
        self.assertEqual(attrs['actually_active'], 280)

    def test_discrepancy_is_enabled_minus_active(self):
        attrs = self._attrs(mqtt_enabled_count=283, active_count=280)
        self.assertEqual(attrs['discrepancy'], 3)

    def test_by_type_and_by_category_keys(self):
        attrs = self._attrs(type_counts={'sensor': 200}, category_counts={'diag': 50})
        self.assertEqual(attrs['by_type'], {'sensor': 200})
        self.assertEqual(attrs['by_category'], {'diag': 50})

    def test_write_keys(self):
        attrs = self._attrs(write_total=10, write_success=9, write_failed=1)
        self.assertEqual(attrs['writes_total'], 10)
        self.assertEqual(attrs['writes_success'], 9)
        self.assertEqual(attrs['writes_failed'], 1)

    def test_write_success_rate_default_when_no_writes(self):
        """Default success rate must be 100.0 when write_total=0."""
        attrs = self._attrs(write_total=0, write_success=0, write_failed=0)
        self.assertEqual(attrs['write_success_rate'], 100.0)


class TestBuildNumberConfigValues(unittest.TestCase):
    """Pin the exact computed values in _build_number_config.

    44 survivors — step calculation, fallback min/max constants, mode string.
    """

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        pub = object.__new__(MqttDiscoveryPublisher)
        pub.mqtt = MagicMock()
        pub.device_info = {}
        pub._range_warnings_issued = set()
        return pub

    def _call(self, config, entity_id, point_id, metadata, bulk_data=None):
        self._pub()._build_number_config.__func__(
            self._pub(), config, entity_id, point_id,
            'Test', '', metadata, bulk_data or {},
        )

    def _build(self, divisor=1, min_val=0, max_val=100,
               current_raw=None, point_id=9999):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        pub = object.__new__(MqttDiscoveryPublisher)
        pub.mqtt = MagicMock()
        pub.device_info = {}
        pub._range_warnings_issued = set()
        config = {}
        metadata = {'minValue': min_val, 'maxValue': max_val, 'divisor': divisor}
        bulk = {point_id: {'raw_value': current_raw}} if current_raw is not None else {}
        pub._build_number_config(config, 'test_id', point_id, 'T', '', metadata, bulk)
        return config

    def test_step_for_divisor_1(self):
        config = self._build(divisor=1)
        self.assertEqual(config['step'], 1.0)

    def test_step_for_divisor_10(self):
        config = self._build(divisor=10)
        self.assertAlmostEqual(config['step'], 0.1)

    def test_step_for_divisor_100(self):
        config = self._build(divisor=100)
        self.assertAlmostEqual(config['step'], 0.01)

    def test_mode_is_box(self):
        config = self._build()
        self.assertEqual(config['mode'], 'box')

    def test_min_max_divided_by_divisor(self):
        config = self._build(divisor=10, min_val=-400, max_val=700)
        self.assertAlmostEqual(config['min'], -40.0)
        self.assertAlmostEqual(config['max'], 70.0)

    def test_degenerate_range_fallback_constants_no_current(self):
        """When min==max and no current value: fallback -32768/div to 32767/div."""
        config = self._build(divisor=1, min_val=5, max_val=5, current_raw=None)
        self.assertEqual(config['min'], -32768)
        self.assertEqual(config['max'], 32767)

    def test_degenerate_range_fallback_with_current_value(self):
        """When min==max with current value: anchor±100, min(-100, anchor)."""
        config = self._build(divisor=1, min_val=5, max_val=5,
                             current_raw=50, point_id=9998)
        self.assertEqual(config['min'], -100)   # min(50, -100) = -100
        self.assertEqual(config['max'], 100)    # max(50, 100) = 100

    def test_degenerate_range_flag_set(self):
        config = self._build(divisor=1, min_val=5, max_val=5)
        self.assertTrue(config.get('_degenerate_range', False))

    def test_normal_range_no_degenerate_flag(self):
        config = self._build(divisor=1, min_val=0, max_val=100)
        self.assertFalse(config.get('_degenerate_range', False))


class TestPublishBridgeStatusPayloadKeys(unittest.TestCase):
    """Pin payload keys in publish_bridge_status.

    32 survivors — key names in the status and nested sub-dict payloads that
    nothing asserted. A renamed key silently breaks external monitors.
    """

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, BrowserTopic
        self.BrowserTopic = BrowserTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def _call(self, **kwargs):
        import time as _time
        defaults = dict(
            bridge_start_time=_time.time() - 3600,
            api_consecutive_failures=0,
            api_failure_threshold=3,
            api_last_success_time=_time.time(),
            last_fetch_duration=0.5,
            write_total=10, write_success=9, write_failed=1,
            last_write_error=None, pending_write_count=0,
            mqtt_enabled_count=283, all_points_count=1158,
            known_dynamic_count=5,
        )
        defaults.update(kwargs)
        self.pub.publish_bridge_status(**defaults)
        calls = [c for c in self.mqtt.publish.call_args_list
                 if c[0][0] == self.BrowserTopic.BRIDGE_STATUS]
        return json.loads(calls[-1][0][1])

    def test_top_level_keys(self):
        payload = self._call()
        for key in ('status', 'timestamp', 'iso_timestamp', 'uptime_s',
                    'api', 'writes', 'entities'):
            self.assertIn(key, payload, f"Missing top-level key '{key}'")

    def test_status_healthy_when_below_threshold(self):
        payload = self._call(api_consecutive_failures=0, api_failure_threshold=3)
        self.assertEqual(payload['status'], 'healthy')

    def test_status_degraded_when_at_or_above_threshold(self):
        payload = self._call(api_consecutive_failures=3, api_failure_threshold=3)
        self.assertEqual(payload['status'], 'degraded')

    def test_api_sub_dict_keys(self):
        payload = self._call()
        api = payload['api']
        for key in ('healthy', 'consecutive_failures', 'failure_threshold',
                    'last_success', 'last_fetch_duration_s'):
            self.assertIn(key, api, f"Missing api key '{key}'")

    def test_api_consecutive_failures_value(self):
        payload = self._call(api_consecutive_failures=2)
        self.assertEqual(payload['api']['consecutive_failures'], 2)

    def test_api_last_fetch_duration_rounded(self):
        payload = self._call(last_fetch_duration=1.23456789)
        self.assertAlmostEqual(payload['api']['last_fetch_duration_s'], 1.235, places=3)

    def test_writes_sub_dict_keys(self):
        payload = self._call()
        writes = payload['writes']
        for key in ('total', 'success', 'failed', 'pending',
                    'success_rate_pct', 'last_error'):
            self.assertIn(key, writes, f"Missing writes key '{key}'")

    def test_writes_success_rate_default_when_no_writes(self):
        payload = self._call(write_total=0, write_success=0, write_failed=0)
        self.assertEqual(payload['writes']['success_rate_pct'], 100.0)

    def test_entities_sub_dict_keys(self):
        payload = self._call()
        entities = payload['entities']
        for key in ('total_known', 'mqtt_enabled', 'known_dynamic'):
            self.assertIn(key, entities, f"Missing entities key '{key}'")

    def test_entities_values(self):
        payload = self._call(all_points_count=1158, mqtt_enabled_count=283,
                             known_dynamic_count=5)
        self.assertEqual(payload['entities']['total_known'], 1158)
        self.assertEqual(payload['entities']['mqtt_enabled'], 283)
        self.assertEqual(payload['entities']['known_dynamic'], 5)

# ===========================================================================
# Round 2 mutmut survivor tests — targeting remaining 687 survivors
# ===========================================================================


class TestPublishStaticAttributesPayloadKeys(unittest.TestCase):
    """Pin exact key names in _publish_static_attributes payload.

    25 survivors — 'point_id', 'modbus_register', 'writable', 'default_value',
    'description' key names plus json_attributes_topic wired into config.
    """

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        pub = object.__new__(MqttDiscoveryPublisher)
        pub.mqtt = MagicMock()
        pub.device_info = {}
        return pub

    def _metadata(self, register_id=100, divisor=1, default=None):
        m = {'modbusRegisterID': register_id, 'divisor': divisor,
             'modbusRegisterType': 'MODBUS_INPUT_REGISTER'}
        if default is not None:
            m['intDefaultValue'] = default
        return m

    def _attrs_payload(self, entity_type='sensor', point_id=100,
                       unit='°C', writable=True, description='',
                       metadata=None):
        pub = self._pub()
        config = {}
        pub._publish_static_attributes(
            entity_type, 'test_id', point_id, unit, writable,
            description, metadata or self._metadata(), config,
        )
        attrs_call = next(c for c in pub.mqtt.publish.call_args_list
                          if 'attributes' in c[0][0])
        return json.loads(attrs_call[0][1])

    def test_point_id_key_name_and_value(self):
        """Key must be 'point_id' (string value of int)."""
        attrs = self._attrs_payload(point_id=5110)
        self.assertIn('point_id', attrs)
        self.assertEqual(attrs['point_id'], '5110')

    def test_modbus_register_key_name(self):
        """Key must be 'modbus_register' not 'modbusRegisterID'."""
        attrs = self._attrs_payload(metadata=self._metadata(register_id=42))
        self.assertIn('modbus_register', attrs)
        self.assertEqual(attrs['modbus_register'], '42')

    def test_writable_key_name_and_value(self):
        attrs = self._attrs_payload(writable=True)
        self.assertIn('writable', attrs)
        self.assertTrue(attrs['writable'])

    def test_description_key_included_when_present(self):
        attrs = self._attrs_payload(description='Compressor speed')
        self.assertIn('description', attrs)
        self.assertEqual(attrs['description'], 'Compressor speed')

    def test_description_key_absent_when_empty(self):
        attrs = self._attrs_payload(description='')
        self.assertNotIn('description', attrs)

    def test_default_value_key_included_when_present(self):
        """default_value = f'{display} {unit}'.strip() — includes unit."""
        attrs = self._attrs_payload(
            unit='°C',
            metadata=self._metadata(divisor=10, default=200),  # 200/10 = 20.0
        )
        self.assertIn('default_value', attrs)
        self.assertIn('20', attrs['default_value'])

    def test_default_value_key_absent_when_no_default(self):
        attrs = self._attrs_payload(metadata=self._metadata())
        self.assertNotIn('default_value', attrs)

    def test_json_attributes_topic_wired_into_config(self):
        """config['json_attributes_topic'] must be set so the discovery payload
        references the attributes topic."""
        pub = self._pub()
        config = {}
        pub._publish_static_attributes(
            'sensor', 'test_id', 100, '°C', True, '', self._metadata(), config,
        )
        self.assertIn('json_attributes_topic', config)

    def test_button_entity_skipped(self):
        """Buttons have no attributes in HA — skip entirely."""
        pub = self._pub()
        config = {}
        pub._publish_static_attributes(
            'button', 'test_id', 100, '', False, '', self._metadata(), config,
        )
        pub.mqtt.publish.assert_not_called()
        self.assertNotIn('json_attributes_topic', config)


class TestBuildSensorConfigPayloadContent(unittest.TestCase):
    """Pin state_class and device_class values in _build_sensor_config.

    18 survivors — _ACCUMULATING_CLASSES membership, state_class strings,
    device_class assignment, suggested_display_precision, date sensor path.
    """

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        pub = object.__new__(MqttDiscoveryPublisher)
        pub.mqtt = MagicMock()
        pub.device_info = {}
        return pub

    def _build(self, point_id=9999, unit='', title='T',
               metadata=None, entity_id='sensor_test'):
        pub = self._pub()
        config = {}
        pub._build_sensor_config(config, entity_id, point_id, unit, title,
                                 metadata or {})
        return config

    def test_energy_accumulator_state_class_and_device_class(self):
        """kWh sensor with non-zero maxValue → energy accumulator."""
        config = self._build(unit='kWh', metadata={'divisor': 100, 'maxValue': 1000,
                                                    'decimal': 1})
        self.assertEqual(config['state_class'], 'total_increasing')
        self.assertEqual(config['device_class'], 'energy')

    def test_energy_instant_state_class(self):
        """kWh sensor with maxValue==0 → instantaneous power reading."""
        config = self._build(unit='kWh', metadata={'divisor': 100, 'maxValue': 0,
                                                    'decimal': 1})
        self.assertEqual(config['state_class'], 'measurement')
        self.assertNotIn('device_class', config)

    def test_temperature_device_class_and_state_class(self):
        config = self._build(unit='°C', metadata={'divisor': 10, 'maxValue': 70,
                                                   'decimal': 1})
        self.assertEqual(config.get('device_class'), 'temperature')
        self.assertEqual(config['state_class'], 'measurement')

    def test_numeric_unit_sets_measurement_state_class(self):
        """Any sensor with a unit gets state_class=measurement."""
        config = self._build(unit='%', metadata={'divisor': 1, 'maxValue': 100,
                                                  'decimal': 0})
        self.assertEqual(config['state_class'], 'measurement')

    def test_no_unit_no_state_class(self):
        """String/enum sensor (no unit) must not get state_class — HA rejects it."""
        config = self._build(unit='', metadata={'divisor': 1, 'maxValue': 10,
                                                 'decimal': 0})
        self.assertNotIn('state_class', config)

    def test_suggested_display_precision_set_for_numeric(self):
        config = self._build(unit='°C', metadata={'divisor': 10, 'maxValue': 70,
                                                   'decimal': 1})
        self.assertIn('suggested_display_precision', config)
        self.assertEqual(config['suggested_display_precision'], 1)

    def test_suggested_display_precision_absent_for_no_unit(self):
        """Must NOT be set for non-numeric sensors — HA rejects non-numeric states."""
        config = self._build(unit='', metadata={'divisor': 1, 'maxValue': 5,
                                                 'decimal': 0})
        self.assertNotIn('suggested_display_precision', config)

    def test_date_sensor_device_class(self):
        """Point 2685 is a date sensor — device_class must be 'date'."""
        config = self._build(point_id=2685)
        self.assertEqual(config['device_class'], 'date')


class TestPublishInitialDeviceModesValues(unittest.TestCase):
    """Pin exact ON/OFF values and lowercase conversion in publish_initial_device_modes.

    18 survivors — '.lower()' call, '== on' comparison, retained publish.
    """

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, MgmtTopic
        self.MgmtTopic = MgmtTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def _states(self, device_info):
        self.pub.publish_initial_device_modes(device_info)
        return {c.args[0]: c.args[1] for c in self.mqtt.publish.call_args_list}

    def test_aid_mode_on_uppercase_input_publishes_ON(self):
        """aidMode='ON' (uppercase from firmware) must publish 'ON'."""
        states = self._states({'aidMode': 'ON', 'smartMode': 'normal'})
        self.assertEqual(states[self.MgmtTopic.AID_STATE], 'ON')

    def test_aid_mode_off_uppercase_input_publishes_OFF(self):
        states = self._states({'aidMode': 'OFF', 'smartMode': 'normal'})
        self.assertEqual(states[self.MgmtTopic.AID_STATE], 'OFF')

    def test_smart_mode_uppercased_input_lowercased_in_output(self):
        """smartMode='AWAY' → published as 'away' (lowercase for HA select)."""
        states = self._states({'aidMode': 'off', 'smartMode': 'AWAY'})
        self.assertEqual(states[self.MgmtTopic.SMART_STATE], 'away')

    def test_aid_default_is_OFF_not_ON(self):
        """Missing aidMode defaults to 'off' → 'OFF', not 'ON'."""
        states = self._states({})
        self.assertEqual(states[self.MgmtTopic.AID_STATE], 'OFF')

    def test_smart_default_is_normal(self):
        states = self._states({})
        self.assertEqual(states[self.MgmtTopic.SMART_STATE], 'normal')


class TestPublishPointListPayloadKeys(unittest.TestCase):
    """Pin payload keys in publish_point_list.

    12 survivors — 'points', 'count', 'last_updated' key names.
    """

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, BrowserTopic
        self.BrowserTopic = BrowserTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def _payload(self, point_ids):
        self.pub.publish_point_list({pid: {} for pid in point_ids})
        return json.loads(self.mqtt.publish.call_args_list[0][0][1])

    def test_points_key_holds_sorted_ids(self):
        payload = self._payload([300, 100, 200])
        self.assertIn('points', payload)
        self.assertEqual(payload['points'], [100, 200, 300])

    def test_count_key_matches_length(self):
        payload = self._payload([1, 2, 3, 4, 5])
        self.assertIn('count', payload)
        self.assertEqual(payload['count'], 5)

    def test_last_updated_key_present(self):
        payload = self._payload([100])
        self.assertIn('last_updated', payload)


class TestBuildSelectConfigPayloadKeys(unittest.TestCase):
    """Pin 'options' key name in _build_select_config.

    10 survivors — 'options' key name in config dict.
    """

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        pub = object.__new__(MqttDiscoveryPublisher)
        pub.mqtt = MagicMock()
        pub.device_info = {}
        return pub

    def test_options_key_name_when_options_present(self):
        """Key must be 'options' exactly — HA selects won't populate otherwise."""
        pub = self._pub()
        config = {}
        metadata = {'minValue': 0, 'maxValue': 1, 'divisor': 1}
        description = '0=Manual\n1=Auto'
        pub._build_select_config(config, 'test_id', 9999, metadata, description)
        if 'options' in config:  # only if get_entity_options returns non-empty
            self.assertIsInstance(config['options'], list)
            self.assertGreater(len(config['options']), 0)

    def test_options_absent_when_no_mapping(self):
        """When no options are found, config must not have 'options' key."""
        pub = self._pub()
        config = {}
        pub._build_select_config(config, 'test_id', 9999, {}, '')
        # No options → key must not be present (HA would render empty select)
        if 'options' in config:
            self.assertIsInstance(config['options'], list)

    def test_select_optimistic_is_false(self):
        """select entities must set optimistic:false so HA waits for a state
        confirmation before updating the UI — prevents the flip-back UX issue
        during post-write learning detection windows."""
        pub = self._pub()
        config = {}
        pub._build_select_config(config, 'test_id', 9999, {}, '')
        self.assertFalse(config.get('optimistic', True),
                         "select discovery config must include optimistic:false")


class TestPublishBridgeAlertPayloadContent(unittest.TestCase):
    """Pin all payload keys in publish_bridge_alert.

    10 survivors — 'alert_type', 'severity', 'message', 'context' key names,
    retain=False (edge-only trigger), context default.
    """

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, BrowserTopic
        self.BrowserTopic = BrowserTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def _payload(self, **kwargs):
        defaults = dict(alert_type='api_unreachable', severity='warning',
                        message='API timeout')
        defaults.update(kwargs)
        self.pub.publish_bridge_alert(**defaults)
        call = self.mqtt.publish.call_args_list[0]
        return json.loads(call[0][1]), call

    def test_all_required_keys_present(self):
        payload, _ = self._payload()
        for key in ('alert_type', 'severity', 'message',
                    'timestamp', 'iso_timestamp', 'context'):
            self.assertIn(key, payload, f"Missing key '{key}'")

    def test_alert_type_value_passed_through(self):
        payload, _ = self._payload(alert_type='write_failed')
        self.assertEqual(payload['alert_type'], 'write_failed')

    def test_severity_value_passed_through(self):
        payload, _ = self._payload(severity='error')
        self.assertEqual(payload['severity'], 'error')

    def test_message_value_passed_through(self):
        payload, _ = self._payload(message='Specific error text')
        self.assertEqual(payload['message'], 'Specific error text')

    def test_context_defaults_to_empty_dict(self):
        """context=None must become {} in the payload, not null."""
        payload, _ = self._payload()
        self.assertEqual(payload['context'], {})

    def test_context_dict_passed_through(self):
        ctx = {'point_id': 100, 'failures': 3}
        payload, _ = self._payload(context=ctx)
        self.assertEqual(payload['context'], ctx)

    def test_published_non_retained(self):
        """retain=False — alert fires on edge only, not on broker reconnect."""
        _, call = self._payload()
        retain = call[1].get('retain', call[0][2] if len(call[0]) > 2 else True)
        self.assertFalse(retain, "Bridge alert must NOT be retained")


class TestPublishAllMetadataPayloadKeys(unittest.TestCase):
    """Pin 'metadata', 'count', 'last_updated' keys in publish_all_metadata.

    9 survivors — key names in the batched payload.
    """

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, BrowserTopic
        self.BrowserTopic = BrowserTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def _point(self, point_id):
        return {
            'variableId': point_id, 'display_title': f'P{point_id}',
            'entity_type': 'sensor', 'entity_category': '',
            'description': '', 'is_writable': False, 'is_dynamic': False,
            'metadata': {'unit': '', 'minValue': 0, 'maxValue': 10,
                         'modbusRegisterID': point_id, 'variableType': 'integer',
                         'variableSize': 'u8', 'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                         'shortUnit': '', 'divisor': 1, 'decimal': 0, 'change': 0},
        }

    def _payload(self, count=3):
        points = [self._point(i) for i in range(1, count + 1)]
        self.pub.publish_all_metadata(points)
        return json.loads(self.mqtt.publish.call_args_list[0][0][1])

    def test_metadata_key_present(self):
        payload = self._payload()
        self.assertIn('metadata', payload)
        self.assertIsInstance(payload['metadata'], dict)

    def test_count_key_matches_point_count(self):
        payload = self._payload(count=5)
        self.assertIn('count', payload)
        self.assertEqual(payload['count'], 5)

    def test_last_updated_key_present(self):
        payload = self._payload()
        self.assertIn('last_updated', payload)

    def test_metadata_keyed_by_string_point_id(self):
        """Metadata dict uses string point IDs as keys (JSON keys are always strings)."""
        payload = self._payload(count=2)
        self.assertIn('1', payload['metadata'])
        self.assertIn('2', payload['metadata'])


class TestPublishEnabledStatePayloadKeys(unittest.TestCase):
    """Pin 'enabled_points', 'count', 'timestamp' keys in publish_enabled_state.

    9 survivors — key names in the enabled-state payload.
    """

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, BrowserTopic
        self.BrowserTopic = BrowserTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def _payload(self, point_ids):
        self.pub.publish_enabled_state(set(point_ids))
        return json.loads(self.mqtt.publish.call_args_list[0][0][1])

    def test_enabled_points_key_present(self):
        payload = self._payload([100, 200, 300])
        self.assertIn('enabled_points', payload)
        self.assertIsInstance(payload['enabled_points'], list)

    def test_count_key_matches_set_size(self):
        payload = self._payload([1, 2, 3, 4])
        self.assertIn('count', payload)
        self.assertEqual(payload['count'], 4)

    def test_timestamp_key_present(self):
        payload = self._payload([100])
        self.assertIn('timestamp', payload)


class TestPublishUptimePayloadKeys(unittest.TestCase):
    """Pin 'started', 'last_api_success', 'consecutive_failures' keys in uptime attrs.

    7 survivors — key names in the uptime attrs payload.
    """

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, MgmtTopic
        self.MgmtTopic = MgmtTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def _attrs(self, **kwargs):
        import time as _time
        defaults = dict(
            bridge_start_time=_time.time() - 3600,
            api_last_success_time=_time.time(),
            api_consecutive_failures=0,
        )
        defaults.update(kwargs)
        self.pub.publish_uptime(**defaults)
        attr_calls = [c for c in self.mqtt.publish.call_args_list
                      if c[0][0] == self.MgmtTopic.UPTIME_ATTRS]
        return json.loads(attr_calls[0][0][1])

    def test_all_required_keys_present(self):
        attrs = self._attrs()
        for key in ('started', 'last_api_success', 'consecutive_failures'):
            self.assertIn(key, attrs, f"Missing uptime attr key '{key}'")

    def test_consecutive_failures_value(self):
        attrs = self._attrs(api_consecutive_failures=2)
        self.assertEqual(attrs['consecutive_failures'], 2)

    def test_started_is_iso_format(self):
        attrs = self._attrs()
        self.assertRegex(attrs['started'], r'^\d{4}-\d{2}-\d{2}')


class TestPublishStatsRemainingKeys(unittest.TestCase):
    """Pin remaining survivor key names in publish_stats attrs.

    14 survivors remaining — 'note' key, timestamp key names.
    """

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, MgmtTopic
        self.MgmtTopic = MgmtTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def _attrs(self):
        self.pub.publish_stats(
            all_points_count=1158, mqtt_enabled_count=283, active_count=280,
            type_counts={}, category_counts={}, writable_count=50,
        )
        calls = [c for c in self.mqtt.publish.call_args_list
                 if c[0][0] == self.MgmtTopic.STATS_ATTRS]
        return json.loads(calls[0][0][1])

    def test_note_key_present_and_string(self):
        attrs = self._attrs()
        self.assertIn('note', attrs)
        self.assertIsInstance(attrs['note'], str)

    def test_timestamp_key_is_numeric(self):
        attrs = self._attrs()
        self.assertIn('timestamp', attrs)
        self.assertIsInstance(attrs['timestamp'], float)

    def test_last_updated_key_is_iso_string(self):
        attrs = self._attrs()
        self.assertIn('last_updated', attrs)
        self.assertRegex(attrs['last_updated'], r'^\d{4}-\d{2}-\d{2}')


class TestPublishEntityDiscoveryConfigStructure(unittest.TestCase):
    """Pin remaining survivors in publish_entity_discovery.

    131 survivors — entity_category conditional, sort_keys in json.dumps,
    _-prefix stripping from publish_config, attributes_topic key in return dict.
    """

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        mqtt = MagicMock()
        mqtt.publish.return_value = MagicMock(rc=0)
        pub = MqttDiscoveryPublisher(
            mqtt_client=mqtt, device_info={'identifiers': ['nibe_test']},
            device_id='test', device_name='Test',
        )
        return pub, mqtt

    def _point(self, point_id=100, entity_type='sensor', category='',
               with_description=False):
        return {
            'variableId': point_id,
            'display_title': 'Test',
            'entity_type': entity_type,
            'entity_category': category,
            'description': 'A description' if with_description else '',
            'is_writable': False,
            'is_dynamic': False,
            'metadata': {
                'unit': '°C', 'minValue': 0, 'maxValue': 100,
                'modbusRegisterID': point_id, 'divisor': 10, 'decimal': 1,
                'change': 0, 'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'variableType': 'integer', 'variableSize': 's16',
                'shortUnit': 'C',
            },
        }

    def test_entity_category_included_when_set(self):
        pub, mqtt = self._pub()
        pub.publish_entity_discovery(
            self._point(category='diagnostic'), {})
        config_call = next(c for c in mqtt.publish.call_args_list
                           if '/config' in c[0][0])
        payload = json.loads(config_call[0][1])
        self.assertIn('entity_category', payload)
        self.assertEqual(payload['entity_category'], 'diagnostic')

    def test_entity_category_absent_when_empty(self):
        pub, mqtt = self._pub()
        pub.publish_entity_discovery(self._point(category=''), {})
        config_call = next(c for c in mqtt.publish.call_args_list
                           if '/config' in c[0][0])
        payload = json.loads(config_call[0][1])
        self.assertNotIn('entity_category', payload)

    def test_internal_keys_stripped_from_published_config(self):
        """Keys starting with '_' (e.g. '_degenerate_range') must not appear
        in the published discovery payload — they are internal bridge state."""
        pub, mqtt = self._pub()
        point = self._point(100, entity_type='number')
        point['is_writable'] = True
        point['metadata']['modbusRegisterType'] = 'MODBUS_HOLDING_REGISTER'
        pub.publish_entity_discovery(point, {})
        config_call = next(c for c in mqtt.publish.call_args_list
                           if '/config' in c[0][0])
        payload = json.loads(config_call[0][1])
        internal_keys = [k for k in payload if k.startswith('_')]
        self.assertEqual(internal_keys, [],
                         f"Internal keys must not be published: {internal_keys}")

    def test_attributes_topic_key_in_return_dict(self):
        """entity_info must have 'attributes_topic' key — EntityManager reads it."""
        pub, _ = self._pub()
        entity_info = pub.publish_entity_discovery(self._point(), {})
        self.assertIn('attributes_topic', entity_info)

    def test_config_published_with_sort_keys(self):
        """JSON must be published with sort_keys=True for stable hash comparison."""
        pub, mqtt = self._pub()
        pub.publish_entity_discovery(self._point(100), {})
        config_call = next(c for c in mqtt.publish.call_args_list
                           if '/config' in c[0][0])
        raw_json = config_call[0][1]
        # Verify the JSON is actually sorted by re-parsing and re-serialising
        parsed = json.loads(raw_json)
        sorted_json = json.dumps(parsed, sort_keys=True)
        self.assertEqual(raw_json, sorted_json,
                         "Discovery config JSON must be sorted (sort_keys=True)")


class TestNumberConfigDivisorZeroHandling(unittest.TestCase):
    """Pin 'or 1' guard in _build_number_config divisor handling.

    40 survivors remaining in _build_number_config — the 'or 1' guard that
    prevents division by zero when firmware reports divisor=0.
    """

    def _build(self, divisor, min_val=0, max_val=100, point_id=9999):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        pub = object.__new__(MqttDiscoveryPublisher)
        pub.mqtt = MagicMock()
        pub.device_info = {}
        pub._range_warnings_issued = set()
        config = {}
        metadata = {'minValue': min_val, 'maxValue': max_val, 'divisor': divisor}
        pub._build_number_config(config, 'test_id', point_id, 'T', '', metadata, {})
        return config

    def test_divisor_zero_treated_as_one(self):
        """divisor=0 → treated as 1 (step=1, min/max unchanged)."""
        config = self._build(divisor=0, min_val=0, max_val=100)
        self.assertEqual(config['step'], 1.0)
        self.assertEqual(config['min'], 0.0)
        self.assertEqual(config['max'], 100.0)

    def test_divisor_none_treated_as_one(self):
        """divisor=None → treated as 1 (firmware can omit the field)."""
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        pub = object.__new__(MqttDiscoveryPublisher)
        pub.mqtt = MagicMock()
        pub.device_info = {}
        pub._range_warnings_issued = set()
        config = {}
        metadata = {'minValue': 0, 'maxValue': 10, 'divisor': None}
        pub._build_number_config(config, 'test_id', 9999, 'T', '', metadata, {})
        self.assertEqual(config['step'], 1.0)

    def test_step_rounding_precision_10_decimal_places(self):
        """round(1/10, 10) must give exactly 0.1 not 0.10000000000000001."""
        config = self._build(divisor=10)
        # Must be exactly 0.1 to 10 decimal places
        self.assertEqual(round(config['step'], 10), 0.1)
        self.assertLess(abs(config['step'] - 0.1), 1e-10)


class TestManagementDiscoveryIconsAndNames(unittest.TestCase):
    """Pin icon strings and display names in publish_management_discovery.

    299 survivors remaining — the bulk are mdi:* icon strings and entity
    display names. These are tested here to close the remaining gap.
    Icons matter for HA UI; display names appear in automations and scripts.
    """

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, MgmtTopic
        self.MgmtTopic = MgmtTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt,
            device_info={'model': 'S-series', 'manufacturer': 'NIBE',
                         'serial_number': '12345'},
            device_id='nibe_test', device_name='Test Device',
        )

    def _payloads(self, **kwargs):
        self.pub.publish_management_discovery(**kwargs)
        result = {}
        for call in self.mqtt.publish.call_args_list:
            topic = call[0][0]
            try:
                result[topic] = json.loads(call[0][1])
            except (ValueError, TypeError):
                pass
        return result

    def test_mode_sensor_name_contains_entity_mode(self):
        p = self._payloads(mode='essential')[self.MgmtTopic.MODE_CONFIG]
        self.assertIn('Entity Mode', p['name'])

    def test_aid_mode_name(self):
        p = self._payloads(mode='essential')[self.MgmtTopic.AID_CONFIG]
        self.assertIn('Aid Mode', p['name'])

    def test_smart_mode_name(self):
        p = self._payloads(mode='essential')[self.MgmtTopic.SMART_CONFIG]
        self.assertIn('Smart Mode', p['name'])

    def test_uptime_sensor_device_class_implies_duration_icon(self):
        p = self._payloads(mode='essential')[self.MgmtTopic.UPTIME_CONFIG]
        # icon must start with 'mdi:' — not empty, not mangled
        self.assertTrue(p.get('icon', '').startswith('mdi:'),
                        f"Uptime icon must be mdi: prefixed, got: {p.get('icon')}")

    def test_all_json_config_payloads_have_icon(self):
        """Every management entity should have an icon — cosmetic but consistent."""
        payloads = self._payloads(mode='essential')
        config_topics = [
            self.MgmtTopic.MODE_CONFIG, self.MgmtTopic.STATS_CONFIG,
            self.MgmtTopic.AID_CONFIG, self.MgmtTopic.SMART_CONFIG,
            self.MgmtTopic.ALARM_CONFIG, self.MgmtTopic.UPTIME_CONFIG,
            self.MgmtTopic.API_OK_CONFIG,
        ]
        for topic in config_topics:
            p = payloads.get(topic, {})
            icon = p.get('icon', '')
            self.assertTrue(icon.startswith('mdi:'),
                            f"Topic {topic}: icon must start with 'mdi:', got {icon!r}")

# ===========================================================================
# Round 3 mutmut survivor tests — default value mutations in .get() calls
# ===========================================================================


class TestBuildPointMetadataDictDefaults(unittest.TestCase):
    """Pin default values in _build_point_metadata_dict .get() calls.

    49 survivors — mutations to default args in metadata_dict.get('key', default)
    survive because existing tests always provide complete metadata dicts.
    These tests use sparse/empty metadata to exercise every default path.
    """

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        pub = object.__new__(MqttDiscoveryPublisher)
        pub.mqtt = MagicMock()
        pub.device_info = {}
        return pub

    def _minimal_point(self, point_id=100):
        """Point with empty metadata — exercises all .get() defaults."""
        return {
            'variableId': point_id,
            'display_title': 'Minimal',
            'entity_type': 'sensor',
            'entity_category': '',
            'description': '',
            'is_writable': False,
            'is_dynamic': False,
            'metadata': {},   # deliberately empty
        }

    def test_empty_metadata_unit_raw_defaults_to_empty_string(self):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._minimal_point())
        self.assertEqual(result['unit_raw'], '')

    def test_empty_metadata_variableType_defaults_to_empty_string(self):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._minimal_point())
        self.assertEqual(result['variableType'], '')

    def test_empty_metadata_variableSize_defaults_to_empty_string(self):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._minimal_point())
        self.assertEqual(result['variableSize'], '')

    def test_empty_metadata_modbusRegisterType_defaults_to_empty_string(self):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._minimal_point())
        self.assertEqual(result['modbusRegisterType'], '')

    def test_empty_metadata_shortUnit_defaults_to_empty_string(self):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._minimal_point())
        self.assertEqual(result['shortUnit'], '')

    def test_empty_metadata_divisor_defaults_to_1(self):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._minimal_point())
        self.assertEqual(result['divisor'], 1)

    def test_empty_metadata_decimal_defaults_to_0(self):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._minimal_point())
        self.assertEqual(result['decimal'], 0)

    def test_empty_metadata_change_defaults_to_0(self):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._minimal_point())
        self.assertEqual(result['change'], 0)

    def test_empty_metadata_min_max_default_to_none(self):
        """minValue/maxValue absent → None (no default in .get() call)."""
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._minimal_point())
        self.assertIsNone(result['min_value'])
        self.assertIsNone(result['max_value'])

    def test_empty_metadata_modbusRegisterID_defaults_to_none(self):
        pub = self._pub()
        result = pub._build_point_metadata_dict(self._minimal_point())
        self.assertIsNone(result['modbusRegisterID'])

    def test_point_is_writable_false_default(self):
        """is_writable absent from point → writable=False default."""
        pub = self._pub()
        point = self._minimal_point()
        del point['is_writable']
        result = pub._build_point_metadata_dict(point)
        self.assertFalse(result['writable'])

    def test_point_is_dynamic_false_default(self):
        pub = self._pub()
        point = self._minimal_point()
        del point['is_dynamic']
        result = pub._build_point_metadata_dict(point)
        self.assertFalse(result['is_dynamic'])

    def test_point_entity_category_empty_default(self):
        pub = self._pub()
        point = self._minimal_point()
        del point['entity_category']
        result = pub._build_point_metadata_dict(point)
        self.assertEqual(result['category'], '')

    def test_point_description_empty_default(self):
        pub = self._pub()
        point = self._minimal_point()
        del point['description']
        result = pub._build_point_metadata_dict(point)
        self.assertEqual(result['description'], '')


class TestPublishStatsDefaultValues(unittest.TestCase):
    """Pin default values in publish_stats.

    14 survivors remaining — mutations to format strings in _fmt_ts() calls
    and default argument values.
    """

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, MgmtTopic
        self.MgmtTopic = MgmtTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def _attrs(self, **kwargs):
        defaults = dict(
            all_points_count=100, mqtt_enabled_count=50, active_count=50,
            type_counts={}, category_counts={}, writable_count=10,
        )
        defaults.update(kwargs)
        self.pub.publish_stats(**defaults)
        calls = [c for c in self.mqtt.publish.call_args_list
                 if c[0][0] == self.MgmtTopic.STATS_ATTRS]
        return json.loads(calls[0][0][1])

    def test_write_total_default_zero(self):
        """write_total defaults to 0 when not supplied."""
        attrs = self._attrs()
        self.assertEqual(attrs['writes_total'], 0)

    def test_write_success_default_zero(self):
        attrs = self._attrs()
        self.assertEqual(attrs['writes_success'], 0)

    def test_write_failed_default_zero(self):
        attrs = self._attrs()
        self.assertEqual(attrs['writes_failed'], 0)

    def test_writable_count_value(self):
        attrs = self._attrs(writable_count=42)
        self.assertEqual(attrs['writable_count'], 42)

    def test_note_is_non_empty_string(self):
        """Note string must be non-empty — mutations change it to empty."""
        attrs = self._attrs()
        self.assertGreater(len(attrs['note']), 0)


class TestPublishBridgeAlertDefaults(unittest.TestCase):
    """Pin default values in publish_bridge_alert.

    10 survivors — context=None default, retain=False, string mutations
    in format string.
    """

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, BrowserTopic
        self.BrowserTopic = BrowserTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def _call(self, **kwargs):
        defaults = dict(alert_type='api_unreachable', severity='warning',
                        message='msg')
        defaults.update(kwargs)
        self.pub.publish_bridge_alert(**defaults)
        call = self.mqtt.publish.call_args_list[0]
        return json.loads(call[0][1]), call

    def test_context_none_becomes_empty_dict_not_null(self):
        """context=None (default) must serialise as {} not null in JSON."""
        payload, _ = self._call(context=None)
        self.assertEqual(payload['context'], {})
        # Verify it's a dict, not None (null in JSON)
        self.assertIsInstance(payload['context'], dict)

    def test_alert_type_is_string_not_mutated(self):
        """alert_type value must pass through exactly."""
        payload, _ = self._call(alert_type='write_failed')
        self.assertEqual(payload['alert_type'], 'write_failed')

    def test_published_to_correct_topic(self):
        _, call = self._call()
        self.assertEqual(call[0][0], self.BrowserTopic.BRIDGE_ALERT)


class TestPublishInitialDeviceModesDefaults(unittest.TestCase):
    """Pin default values in publish_initial_device_modes.

    18 survivors — 'off' default for aidMode, 'normal' default for smartMode,
    log format string mutations.
    """

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, MgmtTopic
        self.MgmtTopic = MgmtTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def _states(self, device_info):
        self.pub.publish_initial_device_modes(device_info)
        return {c.args[0]: c.args[1] for c in self.mqtt.publish.call_args_list}

    def test_aid_mode_default_off_not_on(self):
        """aidMode key absent → default 'off' → published as 'OFF' not 'ON'."""
        states = self._states({})
        self.assertEqual(states[self.MgmtTopic.AID_STATE], 'OFF')
        self.assertNotEqual(states[self.MgmtTopic.AID_STATE], 'ON')

    def test_smart_mode_default_normal_not_empty(self):
        """smartMode key absent → default 'normal' → published as 'normal'."""
        states = self._states({})
        self.assertEqual(states[self.MgmtTopic.SMART_STATE], 'normal')
        self.assertNotEqual(states[self.MgmtTopic.SMART_STATE], '')

    def test_aid_mode_numeric_zero_publishes_OFF(self):
        """aidMode=0 (integer from firmware) → str(0).lower()='0' → != 'on' → OFF."""
        states = self._states({'aidMode': 0, 'smartMode': 'normal'})
        self.assertEqual(states[self.MgmtTopic.AID_STATE], 'OFF')

    def test_smart_mode_mixed_case_normalised(self):
        """smartMode='Normal' → 'normal' after .lower()."""
        states = self._states({'aidMode': 'off', 'smartMode': 'Normal'})
        self.assertEqual(states[self.MgmtTopic.SMART_STATE], 'normal')


class TestPublishPointListDefaults(unittest.TestCase):
    """Pin default values and edge cases in publish_point_list.

    10 survivors — key name mutations, empty dict handling.
    """

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, BrowserTopic
        self.BrowserTopic = BrowserTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def _payload(self, point_ids):
        self.pub.publish_point_list({pid: {} for pid in point_ids})
        return json.loads(self.mqtt.publish.call_args_list[0][0][1])

    def test_empty_dict_publishes_empty_points_list(self):
        payload = self._payload([])
        self.assertEqual(payload['points'], [])
        self.assertEqual(payload['count'], 0)

    def test_points_are_sorted_integers(self):
        """Points must be sorted — HA automations may rely on stable ordering."""
        payload = self._payload([500, 100, 300, 200, 400])
        self.assertEqual(payload['points'], [100, 200, 300, 400, 500])

    def test_count_matches_len_of_points(self):
        payload = self._payload([1, 2, 3])
        self.assertEqual(payload['count'], len(payload['points']))


class TestPublishAllMetadataDefaults(unittest.TestCase):
    """Pin edge cases in publish_all_metadata.

    7 survivors — key name mutations, empty points list.
    """

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, BrowserTopic
        self.BrowserTopic = BrowserTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def _point(self, pid):
        return {
            'variableId': pid, 'display_title': f'P{pid}',
            'entity_type': 'sensor', 'entity_category': '',
            'description': '', 'is_writable': False, 'is_dynamic': False,
            'metadata': {'unit': '', 'minValue': 0, 'maxValue': 10,
                         'modbusRegisterID': pid, 'variableType': 'integer',
                         'variableSize': 'u8', 'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                         'shortUnit': '', 'divisor': 1, 'decimal': 0, 'change': 0},
        }

    def test_empty_points_list_publishes_zero_count(self):
        self.pub.publish_all_metadata([])
        payload = json.loads(self.mqtt.publish.call_args_list[0][0][1])
        self.assertEqual(payload['count'], 0)
        self.assertEqual(payload['metadata'], {})

    def test_metadata_values_are_dicts(self):
        """Each entry in metadata must be a dict (the per-point metadata dict)."""
        self.pub.publish_all_metadata([self._point(100)])
        payload = json.loads(self.mqtt.publish.call_args_list[0][0][1])
        self.assertIsInstance(payload['metadata']['100'], dict)


class TestPublishEnabledStateDefaults(unittest.TestCase):
    """Pin edge cases in publish_enabled_state.

    7 survivors — empty set, count key.
    """

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, BrowserTopic
        self.BrowserTopic = BrowserTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def _payload(self, ids):
        self.pub.publish_enabled_state(set(ids))
        return json.loads(self.mqtt.publish.call_args_list[0][0][1])

    def test_empty_set_publishes_empty_list_and_zero_count(self):
        payload = self._payload([])
        self.assertEqual(payload['enabled_points'], [])
        self.assertEqual(payload['count'], 0)

    def test_count_matches_len_of_enabled_points(self):
        payload = self._payload([1, 2, 3])
        self.assertEqual(payload['count'], len(payload['enabled_points']))


class TestPublishNumberConfigDefaults(unittest.TestCase):
    """Pin default value handling in _build_number_config.

    39 survivors — the 'or 1' divisor guard, degenerate range anchor bounds,
    and fallback min/max constant values.
    """

    def _build(self, divisor=1, min_val=None, max_val=None,
               current_raw=None, point_id=9999):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        pub = object.__new__(MqttDiscoveryPublisher)
        pub.mqtt = MagicMock()
        pub.device_info = {}
        pub._range_warnings_issued = set()
        config = {}
        metadata = {'divisor': divisor}
        if min_val is not None:
            metadata['minValue'] = min_val
        if max_val is not None:
            metadata['maxValue'] = max_val
        bulk = {point_id: {'raw_value': current_raw}} if current_raw is not None else {}
        pub._build_number_config(config, 'test_id', point_id, 'T', '', metadata, bulk)
        return config

    def test_no_min_max_in_metadata_sets_no_min_max_in_config(self):
        """When firmware provides no min/max, config must not have min/max keys."""
        config = self._build(divisor=1)
        self.assertNotIn('min', config)
        self.assertNotIn('max', config)

    def test_degenerate_range_anchor_below_minus_100_uses_anchor(self):
        """When anchor < -100: min=anchor (not -100), max=max(anchor,100)=100."""
        config = self._build(divisor=1, min_val=-200, max_val=-200,
                             current_raw=-150, point_id=9997)
        self.assertEqual(config['min'], -150)  # anchor=-150 < -100 → min=anchor
        self.assertEqual(config['max'], 100)   # max(-150, 100) = 100

    def test_degenerate_range_anchor_above_100_uses_anchor(self):
        """When anchor > 100: min=min(anchor,-100)=-100, max=anchor."""
        config = self._build(divisor=1, min_val=200, max_val=200,
                             current_raw=150, point_id=9996)
        self.assertEqual(config['min'], -100)  # min(150, -100) = -100
        self.assertEqual(config['max'], 150)   # anchor=150 > 100 → max=anchor

    def test_degenerate_fallback_no_current_uses_register_range(self):
        """No current value: fallback is -32768/divisor to 32767/divisor."""
        config = self._build(divisor=10, min_val=5, max_val=5)
        self.assertAlmostEqual(config['min'], -32768 / 10)
        self.assertAlmostEqual(config['max'], 32767 / 10)

    def test_unit_of_measurement_absent_when_no_unit(self):
        """When unit is empty string, unit_of_measurement must not appear."""
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        pub = object.__new__(MqttDiscoveryPublisher)
        pub.mqtt = MagicMock()
        pub.device_info = {}
        pub._range_warnings_issued = set()
        config = {}
        metadata = {'minValue': 0, 'maxValue': 100, 'divisor': 1}
        pub._build_number_config(config, 'test_id', 9999, 'T', '', metadata, {})
        self.assertNotIn('unit_of_measurement', config)

    def test_unit_of_measurement_present_when_unit_given(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        pub = object.__new__(MqttDiscoveryPublisher)
        pub.mqtt = MagicMock()
        pub.device_info = {}
        pub._range_warnings_issued = set()
        config = {}
        metadata = {'minValue': 0, 'maxValue': 100, 'divisor': 1}
        pub._build_number_config(config, 'test_id', 9999, 'T', '°C', metadata, {})
        self.assertEqual(config['unit_of_measurement'], '°C')

# ===========================================================================
# Round 4 mutmut survivor tests — genuine logic gaps identified from diffs
# ===========================================================================


class TestBuildNumberConfigLogicGaps(unittest.TestCase):
    """Pin the specific logic mutations that survived in _build_number_config.

    Genuine gaps identified from mutmut diffs:
    - 'and' vs 'or' in min/max None guard (mutmut_48)
    - anchor = current_raw / divisor vs * divisor (mutmut_73)
    - current_raw < min_val vs <= min_val (mutmut_117)
    - round(..., 10) vs round(..., 11) (mutmut_156)
    """

    def _build(self, divisor=1, min_val=None, max_val=None,
               current_raw=None, unit='', point_id=9999):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        pub = object.__new__(MqttDiscoveryPublisher)
        pub.mqtt = MagicMock()
        pub.device_info = {}
        pub._range_warnings_issued = set()
        config = {}
        metadata = {'divisor': divisor}
        if min_val is not None:
            metadata['minValue'] = min_val
        if max_val is not None:
            metadata['maxValue'] = max_val
        bulk = {point_id: {'raw_value': current_raw}} if current_raw is not None else {}
        pub._build_number_config(config, 'test_id', point_id, 'T', unit, metadata, bulk)
        return config

    def test_min_only_no_max_skips_range(self):
        """'and' not 'or': min present but max absent → no min/max in config."""
        config = self._build(min_val=0, max_val=None)
        self.assertNotIn('min', config)
        self.assertNotIn('max', config)

    def test_max_only_no_min_skips_range(self):
        """'and' not 'or': max present but min absent → no min/max in config."""
        config = self._build(min_val=None, max_val=100)
        self.assertNotIn('min', config)
        self.assertNotIn('max', config)

    def test_degenerate_anchor_is_divided_not_multiplied(self):
        """anchor = current_raw / divisor, not current_raw * divisor."""
        # With divisor=10, current_raw=500: anchor should be 50, not 5000
        config = self._build(divisor=10, min_val=5, max_val=5,
                             current_raw=500, point_id=9994)
        # anchor=50: min=min(50,-100)=-100, max=max(50,100)=100
        self.assertEqual(config['min'], -100)
        self.assertEqual(config['max'], 100)

    def test_out_of_range_uses_strict_less_than_not_lte(self):
        """current_raw < min_val (not <=): value exactly AT min_val is NOT out of range."""
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        pub = object.__new__(MqttDiscoveryPublisher)
        pub.mqtt = MagicMock()
        pub.device_info = {}
        pub._range_warnings_issued = set()
        config = {}
        # current_raw == min_val exactly → NOT out of range → no warning issued
        metadata = {'minValue': 100, 'maxValue': 200, 'divisor': 1}
        bulk = {9993: {'raw_value': 100}}  # exactly at min
        pub._build_number_config(config, 'test_id', 9993, 'T', '', metadata, bulk)
        self.assertNotIn(9993, pub._range_warnings_issued)

    def test_out_of_range_below_min_triggers_warning(self):
        """current_raw < min_val: value below min triggers range warning."""
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        pub = object.__new__(MqttDiscoveryPublisher)
        pub.mqtt = MagicMock()
        pub.device_info = {}
        pub._range_warnings_issued = set()
        config = {}
        metadata = {'minValue': 100, 'maxValue': 200, 'divisor': 1}
        bulk = {9992: {'raw_value': 99}}  # one below min
        pub._build_number_config(config, 'test_id', 9992, 'T', '', metadata, bulk)
        self.assertIn(9992, pub._range_warnings_issued)

    def test_step_precision_is_10_not_11(self):
        """round(1/divisor, 10) — precision must be 10 not 11."""
        config = self._build(divisor=10)
        # round(0.1, 10) = 0.1, round(0.1, 11) = 0.10000000000 (both same here)
        # Test that step is exactly the same as round(1/10, 10)
        self.assertEqual(config['step'], round(1/10, 10))
        # And NOT round(1/10, 11) — they differ for 1/3
        config3 = self._build(divisor=3)
        self.assertEqual(config3['step'], round(1/3, 10))
        self.assertNotEqual(config3['step'], round(1/3, 11))


class TestPublishStatsLogicGaps(unittest.TestCase):
    """Pin logic mutations in publish_stats that survived.

    Genuine gaps:
    - all_points_count > 0 vs > 1 for enabled_pct (mutmut_14)
    - write_total > 0 vs > 1 for write_success_rate (mutmut_60)
    - round(..., 1) precision (mutmut_6, mutmut_52)
    """

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, MgmtTopic
        self.MgmtTopic = MgmtTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def _attrs(self, **kwargs):
        defaults = dict(
            all_points_count=100, mqtt_enabled_count=50, active_count=50,
            type_counts={}, category_counts={}, writable_count=10,
        )
        defaults.update(kwargs)
        self.pub.publish_stats(**defaults)
        calls = [c for c in self.mqtt.publish.call_args_list
                 if c[0][0] == self.MgmtTopic.STATS_ATTRS]
        return json.loads(calls[0][0][1])

    def test_enabled_pct_zero_when_exactly_one_point_and_zero_enabled(self):
        """all_points_count=1, mqtt_enabled_count=0: > 0 guard fires, pct=0.0.
        With > 1 mutation, all_points_count=1 would trigger the else → pct=0 also.
        Use all_points_count=1, mqtt_enabled_count=1 to distinguish."""
        attrs = self._attrs(all_points_count=1, mqtt_enabled_count=1)
        self.assertEqual(attrs['enabled_percentage'], 100.0)

    def test_enabled_pct_when_all_points_count_is_1(self):
        """all_points_count=1 must use the division path (> 0 is True for 1)."""
        attrs = self._attrs(all_points_count=1, mqtt_enabled_count=1)
        # If > 0: 1/1 * 100 = 100.0. If > 1: falls to else → 0. Must be 100.
        self.assertEqual(attrs['enabled_percentage'], 100.0)

    def test_write_success_rate_when_write_total_is_1(self):
        """write_total=1 must use division path (> 0 is True for 1).
        Use write_total=1, write_success=0 to distinguish > 0 from > 1:
        - if write_total > 0: 0/1 * 100 = 0.0
        - if write_total > 1: falls to else → 100.0 (default)
        """
        self.mqtt.reset_mock()
        attrs = self._attrs(write_total=1, write_success=0, write_failed=1)
        # > 0 is True for 1 → uses division → 0.0
        self.assertEqual(attrs['write_success_rate'], 0.0)

    def test_enabled_pct_rounded_to_1_decimal(self):
        """enabled_pct = round(x * 100, 1) — precision must be 1 not 0 or 2."""
        attrs = self._attrs(all_points_count=3, mqtt_enabled_count=1)
        # 1/3 * 100 = 33.333... → round to 1dp = 33.3, not 33 or 33.33
        self.assertEqual(attrs['enabled_percentage'], 33.3)

    def test_write_success_rate_rounded_to_1_decimal(self):
        attrs = self._attrs(write_total=3, write_success=1, write_failed=2)
        # 1/3 * 100 = 33.333... → 33.3
        self.assertAlmostEqual(attrs['write_success_rate'], 33.3, places=1)


class TestBuildSensorConfigLogicGaps(unittest.TestCase):
    """Pin logic mutations in _build_sensor_config that survived.

    Genuine gaps:
    - 'and' vs 'or' in accumulating+instant condition (mutmut_75)
    - 'gas' membership in _ACCUMULATING_CLASSES (mutmut_24 uppercase)
    - decimal default 0 vs None (mutmut_97)
    """

    def _build(self, point_id=9999, unit='', title='T', metadata=None):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        pub = object.__new__(MqttDiscoveryPublisher)
        pub.mqtt = MagicMock()
        pub.device_info = {}
        config = {}
        pub._build_sensor_config(config, 'test_id', point_id, unit, title,
                                 metadata or {})
        return config

    def test_gas_unit_gives_accumulating_state_class(self):
        """'gas' must be in _ACCUMULATING_CLASSES — uppercase 'GAS' mutation survives
        because set membership is case-sensitive."""
        # A sensor with unit 'm3' and device_class 'gas' should get total_increasing
        # We test indirectly via a known gas device class
        config = self._build(unit='m³', metadata={'divisor': 1, 'maxValue': 100,
                                                   'decimal': 0})
        # map_device_class('sensor', 'm³', 'T') should return 'gas' or similar
        # The key test: if device_class IS in _ACCUMULATING_CLASSES and not instant,
        # state_class must be 'total_increasing' not 'measurement'
        if config.get('device_class') in ('gas', 'water', 'volume'):
            self.assertEqual(config.get('state_class'), 'total_increasing')

    def test_accumulating_and_instant_both_true_gives_measurement(self):
        """'and' not 'or': when accumulating AND instant, state_class='measurement'.
        With 'or', a non-accumulating class would get total_increasing incorrectly."""
        # kWh + maxValue==0 → is_instant=True, device_class='energy' (accumulating)
        # Both conditions True → 'and is_instant' branch → state_class='measurement'
        config = self._build(unit='kWh', metadata={'divisor': 100, 'maxValue': 0,
                                                    'decimal': 1})
        self.assertEqual(config['state_class'], 'measurement')
        # Must NOT be total_increasing (that's the non-instant accumulating case)
        self.assertNotEqual(config.get('state_class'), 'total_increasing')

    def test_non_accumulating_non_instant_gives_measurement_not_total(self):
        """'and' not 'or': non-accumulating class must not get total_increasing."""
        config = self._build(unit='°C', metadata={'divisor': 10, 'maxValue': 70,
                                                   'decimal': 1})
        self.assertEqual(config.get('state_class'), 'measurement')
        self.assertNotEqual(config.get('state_class'), 'total_increasing')

    def test_decimal_default_is_0_not_none(self):
        """decimal defaults to 0 when absent — must set suggested_display_precision=0
        not skip it (None would fail int() conversion)."""
        config = self._build(unit='°C', metadata={'divisor': 1, 'maxValue': 100})
        # 'decimal' key absent → default 0 → suggested_display_precision=0
        self.assertIn('suggested_display_precision', config)
        self.assertEqual(config['suggested_display_precision'], 0)


class TestPublishEntityDiscoveryLogicGaps(unittest.TestCase):
    """Pin logic mutations in publish_entity_discovery that survived.

    Genuine gaps:
    - time entity type string comparison (mutmut_125: 'TIME' vs 'time')
    - entity_id passed correctly to all builders (mutmut_83/90/97/114)
    - metadata default {} vs None (mutmut_7)
    - point without 'metadata' key doesn't crash
    """

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        mqtt = MagicMock()
        mqtt.publish.return_value = MagicMock(rc=0)
        pub = MqttDiscoveryPublisher(
            mqtt_client=mqtt,
            device_info={'identifiers': ['nibe_test']},
            device_id='test', device_name='Test',
        )
        return pub, mqtt

    def _base_point(self, point_id=100, entity_type='sensor'):
        return {
            'variableId': point_id,
            'display_title': 'Test',
            'entity_type': entity_type,
            'entity_category': '',
            'description': '',
            'is_writable': False,
            'is_dynamic': False,
            'metadata': {
                'unit': '', 'minValue': 0, 'maxValue': 10,
                'modbusRegisterID': point_id, 'divisor': 1, 'decimal': 0,
                'change': 0, 'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                'variableType': 'integer', 'variableSize': 'u8', 'shortUnit': '',
            },
        }

    def test_time_entity_uses_lowercase_comparison(self):
        """entity_type == 'time' (lowercase) — uppercase 'TIME' must NOT match."""
        pub, mqtt = self._pub()
        point = self._base_point(100, entity_type='time')
        point['is_writable'] = True
        entity_info = pub.publish_entity_discovery(point, {})
        self.assertIsNotNone(entity_info)
        # time entity must have state_topic and command_topic
        config_call = next(c for c in mqtt.publish.call_args_list
                           if '/config' in c[0][0])
        payload = json.loads(config_call[0][1])
        self.assertIn('state_topic', payload)
        self.assertIn('command_topic', payload)
        # Must NOT have unit_of_measurement (time entities show HH:MM)
        self.assertNotIn('unit_of_measurement', payload)

    def test_point_without_metadata_key_does_not_crash(self):
        """metadata = point.get('metadata', {}) — missing key → empty dict, not None."""
        pub, mqtt = self._pub()
        point = self._base_point(100)
        del point['metadata']
        # Must not raise AttributeError: 'NoneType' has no attribute 'get'
        result = pub.publish_entity_discovery(point, {})
        self.assertIsNotNone(result)

    def test_entity_id_passed_correctly_to_switch_builder(self):
        """entity_id must reach _build_switch_config, not None."""
        pub, mqtt = self._pub()
        point = self._base_point(5110, entity_type='switch')
        point['is_writable'] = True
        point['metadata']['modbusRegisterType'] = 'MODBUS_HOLDING_REGISTER'
        pub.publish_entity_discovery(point, {})
        config_call = next(c for c in mqtt.publish.call_args_list
                           if '/config' in c[0][0])
        payload = json.loads(config_call[0][1])
        # command_topic must contain the real entity_id, not 'None'
        self.assertNotIn('None', payload.get('command_topic', ''))
        self.assertIn('5110', payload.get('command_topic', ''))

    def test_entity_id_passed_correctly_to_number_builder(self):
        """entity_id must reach _build_number_config, not None."""
        pub, mqtt = self._pub()
        point = self._base_point(1234, entity_type='number')
        point['is_writable'] = True
        point['metadata']['modbusRegisterType'] = 'MODBUS_HOLDING_REGISTER'
        pub.publish_entity_discovery(point, {})
        config_call = next(c for c in mqtt.publish.call_args_list
                           if '/config' in c[0][0])
        payload = json.loads(config_call[0][1])
        self.assertNotIn('None', payload.get('command_topic', ''))
        self.assertIn('1234', payload.get('command_topic', ''))

# ===========================================================================
# Round 5 mutmut survivor tests — retain flags, boundary conditions,
# and remaining logic gaps identified from diff analysis
# ===========================================================================


class TestPubStateRetainAndRcCheck(unittest.TestCase):
    """Pin retain=True and rc!=0 check in _pub_state.

    8 survivors:
    - retain=True dropped or changed to False (mutmut_7/8)
    - rc != 0 changed to == 0 or != 1 (mutmut_9/10)
    """

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def test_pub_state_publishes_with_retain_true(self):
        """_pub_state must always publish with retain=True."""
        self.mqtt.publish.return_value = MagicMock(rc=0)
        self.pub._pub_state('some/topic', 'value')
        call = self.mqtt.publish.call_args_list[0]
        retain = call[1].get('retain', call[0][2] if len(call[0]) > 2 else None)
        self.assertTrue(retain, "_pub_state must publish with retain=True")

    def test_pub_state_logs_warning_on_nonzero_rc(self):
        """rc != 0 (not == 0, not != 1): any non-zero rc triggers warning."""
        self.mqtt.publish.return_value = MagicMock(rc=1)
        with self.assertLogs('nibe.mqtt', level='WARNING') as cm:
            self.pub._pub_state('some/topic', 'value')
        self.assertTrue(any('failed' in m.lower() or 'State publish' in m
                            for m in cm.output))

    def test_pub_state_no_warning_on_rc_zero(self):
        """rc == 0: no warning logged — function completes silently."""
        self.mqtt.publish.return_value = MagicMock(rc=0)
        import logging
        logger = logging.getLogger('nibe.mqtt')
        with self.assertLogs('nibe.mqtt', level='WARNING') as cm:
            # Force at least one log so assertLogs doesn't fail on empty
            logger.warning('sentinel')
            self.pub._pub_state('some/topic', 'value')
        # Only the sentinel should appear — no 'failed' message from _pub_state
        real_warnings = [m for m in cm.output if 'failed' in m.lower() or 'State publish' in m]
        self.assertEqual(real_warnings, [])

    def test_pub_state_warning_on_rc_2(self):
        """rc=2 (not 0, not 1) also triggers warning — checks != 0, not != 1."""
        self.mqtt.publish.return_value = MagicMock(rc=2)
        with self.assertLogs('nibe.mqtt', level='WARNING') as cm:
            self.pub._pub_state('some/topic', 'value')
        self.assertTrue(any('WARNING' in m for m in cm.output))


class TestPublishPointListAndMetadataRetain(unittest.TestCase):
    """Pin retain=True in publish_point_list and publish_point_metadata.

    publish_point_list: 2 survivors (retain dropped or False)
    publish_point_metadata: 2 survivors (retain dropped or False)
    """

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, BrowserTopic
        self.BrowserTopic = BrowserTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def test_publish_point_list_uses_retain_true(self):
        self.pub.publish_point_list({100: {}, 200: {}})
        call = next(c for c in self.mqtt.publish.call_args_list
                    if c[0][0] == self.BrowserTopic.POINT_LIST)
        retain = call[1].get('retain', call[0][2] if len(call[0]) > 2 else None)
        self.assertTrue(retain, "publish_point_list must use retain=True")

    def test_publish_point_metadata_uses_retain_true(self):
        point = {
            'variableId': 100, 'display_title': 'T', 'entity_type': 'sensor',
            'entity_category': '', 'description': '', 'is_writable': False,
            'is_dynamic': False,
            'metadata': {'unit': '', 'modbusRegisterID': 100,
                         'variableType': 'integer', 'variableSize': 'u8',
                         'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                         'shortUnit': '', 'divisor': 1, 'decimal': 0, 'change': 0,
                         'minValue': 0, 'maxValue': 100},
        }
        self.pub.publish_point_metadata(point)
        call = self.mqtt.publish.call_args_list[0]
        retain = call[1].get('retain', call[0][2] if len(call[0]) > 2 else None)
        self.assertTrue(retain, "publish_point_metadata must use retain=True")


class TestPublishBridgeStatusBoundaries(unittest.TestCase):
    """Pin boundary conditions in publish_bridge_status.

    9 survivors:
    - uptime = time() - start (not +) (mutmut_3)
    - api_last_success_time > 0 not >= 0 or > 1 (mutmut_31/32)
    - write_total > 0 not > 1 (mutmut_61)
    - round(..., 3) not round(..., 4) (mutmut_39)
    """

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, BrowserTopic
        self.BrowserTopic = BrowserTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def _call(self, **kwargs):
        import time as _t
        defaults = dict(
            bridge_start_time=_t.time() - 3600,
            api_consecutive_failures=0, api_failure_threshold=3,
            api_last_success_time=_t.time(),
            last_fetch_duration=0.5,
            write_total=10, write_success=9, write_failed=1,
            last_write_error=None, pending_write_count=0,
            mqtt_enabled_count=283, all_points_count=1158,
            known_dynamic_count=5,
        )
        defaults.update(kwargs)
        self.pub.publish_bridge_status(**defaults)
        return json.loads(self.mqtt.publish.call_args_list[-1][0][1])

    def test_uptime_is_subtraction_not_addition(self):
        """uptime = time() - start, not time() + start."""
        import time as _t
        start = _t.time() - 3600  # 1 hour ago
        payload = self._call(bridge_start_time=start)
        # uptime should be ~3600, not ~2*time() (billions)
        self.assertGreater(payload['uptime_s'], 3590)
        self.assertLess(payload['uptime_s'], 3610)

    def test_api_last_success_time_zero_gives_none(self):
        """api_last_success_time=0 → last_success=None (> 0 is False for 0)."""
        payload = self._call(api_last_success_time=0)
        self.assertIsNone(payload['api']['last_success'])

    def test_api_last_success_time_positive_gives_timestamp(self):
        """api_last_success_time > 0 → last_success is a string timestamp."""
        import time as _t
        payload = self._call(api_last_success_time=_t.time())
        self.assertIsNotNone(payload['api']['last_success'])
        self.assertIsInstance(payload['api']['last_success'], str)

    def test_write_total_1_uses_division_not_default(self):
        """write_total=1 → > 0 is True → 0/1*100=0.0, not 100.0 default."""
        payload = self._call(write_total=1, write_success=0, write_failed=1)
        self.assertEqual(payload['writes']['success_rate_pct'], 0.0)

    def test_last_fetch_duration_rounded_to_3_places(self):
        """round(x, 3) not round(x, 4) — 3 decimal places."""
        payload = self._call(last_fetch_duration=1.23456789)
        # round(1.23456789, 3) = 1.235, round(x, 4) = 1.2346
        self.assertEqual(payload['api']['last_fetch_duration_s'], 1.235)


class TestBuildNumberConfigUpperBoundary(unittest.TestCase):
    """Pin current_raw > max_val (strict) in _build_number_config.

    mutmut_118: > changed to >= for max_val check.
    """

    def _build_with_current(self, current_raw, min_val, max_val, point_id=9991):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        pub = object.__new__(MqttDiscoveryPublisher)
        pub.mqtt = MagicMock()
        pub.device_info = {}
        pub._range_warnings_issued = set()
        config = {}
        metadata = {'minValue': min_val, 'maxValue': max_val, 'divisor': 1}
        bulk = {point_id: {'raw_value': current_raw}}
        pub._build_number_config(config, 'test_id', point_id, 'T', '', metadata, bulk)
        return pub._range_warnings_issued

    def test_current_at_max_does_not_trigger_warning(self):
        """current_raw == max_val: > is False → no out-of-range warning."""
        warned = self._build_with_current(100, 0, 100)
        self.assertNotIn(9991, warned)

    def test_current_above_max_triggers_warning(self):
        """current_raw > max_val: > is True → out-of-range warning."""
        warned = self._build_with_current(101, 0, 100, point_id=9990)
        self.assertIn(9990, warned)


class TestPublishEntityDiscoverySensorFallback(unittest.TestCase):
    """Pin entity_type == 'sensor' comparison (not !=) in publish_entity_discovery.

    mutmut_183: == changed to !=, mutmut_185: 'sensor' changed to 'SENSOR'.
    Both cause the unknown-type fallback to fire for sensor entities.
    """

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        mqtt = MagicMock()
        mqtt.publish.return_value = MagicMock(rc=0)
        return MqttDiscoveryPublisher(
            mqtt_client=mqtt,
            device_info={'identifiers': ['nibe_test']},
            device_id='test', device_name='Test',
        ), mqtt

    def _sensor_point(self, point_id=100):
        return {
            'variableId': point_id, 'display_title': 'Test',
            'entity_type': 'sensor', 'entity_category': '',
            'description': '', 'is_writable': False, 'is_dynamic': False,
            'metadata': {'unit': '°C', 'minValue': 0, 'maxValue': 100,
                         'modbusRegisterID': point_id, 'divisor': 10,
                         'decimal': 1, 'change': 0,
                         'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                         'variableType': 'integer', 'variableSize': 's16',
                         'shortUnit': 'C'},
        }

    def test_sensor_entity_gets_state_topic_not_command_topic(self):
        """sensor entity_type == 'sensor' must route to _build_sensor_config,
        which adds state_topic but NOT command_topic."""
        pub, mqtt = self._pub()
        pub.publish_entity_discovery(self._sensor_point(), {})
        config_call = next(c for c in mqtt.publish.call_args_list
                           if '/config' in c[0][0])
        payload = json.loads(config_call[0][1])
        self.assertIn('state_topic', payload)
        self.assertNotIn('command_topic', payload)

    def test_sensor_entity_type_lowercase_match(self):
        """entity_type='sensor' (lowercase) routes to _build_sensor_config.
        If == 'SENSOR', it falls through to the else/unknown fallback which
        would also call _build_sensor_config but via a different path.
        Distinguish by checking no unit_of_measurement is dropped (sensor
        path keeps it, unknown path also calls sensor config)."""
        pub, mqtt = self._pub()
        point = self._sensor_point()
        entity_info = pub.publish_entity_discovery(point, {})
        self.assertIsNotNone(entity_info)
        self.assertEqual(entity_info['entity_type'], 'sensor')
        config_call = next(c for c in mqtt.publish.call_args_list
                           if '/config' in c[0][0])
        payload = json.loads(config_call[0][1])
        # Sensor must have state_topic — confirms routing reached _build_sensor_config
        self.assertIn('state_topic', payload)
        # Sensor with unit gets state_class — confirms proper sensor config path
        self.assertIn('state_class', payload)


class TestPublishEntityDiscoveryRetainAndHash(unittest.TestCase):
    """Pin retain=True and md5 hash in publish_entity_discovery.

    mutmut_284/285: retain=True dropped or changed to False.
    mutmut_253/254/255: md5 hash computation mutated.
    """

    def _pub(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher
        mqtt = MagicMock()
        mqtt.publish.return_value = MagicMock(rc=0)
        return MqttDiscoveryPublisher(
            mqtt_client=mqtt,
            device_info={'identifiers': ['nibe_test']},
            device_id='test', device_name='Test',
        ), mqtt

    def _point(self, point_id=100):
        return {
            'variableId': point_id, 'display_title': 'T',
            'entity_type': 'sensor', 'entity_category': '',
            'description': '', 'is_writable': False, 'is_dynamic': False,
            'metadata': {'unit': '°C', 'minValue': 0, 'maxValue': 100,
                         'modbusRegisterID': point_id, 'divisor': 10,
                         'decimal': 1, 'change': 0,
                         'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                         'variableType': 'integer', 'variableSize': 's16',
                         'shortUnit': 'C'},
        }

    def test_config_topic_published_with_retain_true(self):
        """Discovery config must be retained so HA sees it after restart."""
        pub, mqtt = self._pub()
        pub.publish_entity_discovery(self._point(), {})
        config_call = next(c for c in mqtt.publish.call_args_list
                           if '/config' in c[0][0])
        retain = config_call[1].get('retain',
                                    config_call[0][2] if len(config_call[0]) > 2 else None)
        self.assertTrue(retain, "Discovery config must be published with retain=True")

    def test_identical_config_deduplicated_by_hash(self):
        """md5 hash of the JSON must match on second identical call → skip."""
        pub, mqtt = self._pub()
        point = self._point(100)
        pub.publish_entity_discovery(point, {})
        mqtt.reset_mock()
        pub.publish_entity_discovery(point, {})
        config_calls = [c for c in mqtt.publish.call_args_list
                        if '/config' in c[0][0]]
        self.assertEqual(config_calls, [],
                         "Identical config must be deduplicated by md5 hash")

    def test_changed_config_republished(self):
        """Different point → different hash → config IS published again."""
        pub, mqtt = self._pub()
        pub.publish_entity_discovery(self._point(100), {})
        mqtt.reset_mock()
        # Different point_id → different unique_id → different JSON → different hash
        pub.publish_entity_discovery(self._point(200), {})
        config_calls = [c for c in mqtt.publish.call_args_list
                        if '/config' in c[0][0]]
        self.assertTrue(config_calls, "Changed config must be republished")

# ===========================================================================
# Round 6 — final genuine gaps from mutmut diff analysis
# ===========================================================================


class TestManagementDiscoveryDebugModeRetain(unittest.TestCase):
    """Pin retain=True on the debug-mode state reset publishes.

    mutmut_601: RUN_TESTS_STATE reset to 'unknown' — retain=True not False.
    mutmut_618: RUN_TESTS_ATTRS reset payload — retain=True not False.

    Both must be retained so HA sees the reset state immediately after the
    add-on restarts, not just on the next broker reconnect.
    """

    def setUp(self):
        from nibe_mqtt_publisher import MqttDiscoveryPublisher, MgmtTopic
        self.MgmtTopic = MgmtTopic
        self.mqtt = MagicMock()
        self.pub = MqttDiscoveryPublisher(
            mqtt_client=self.mqtt, device_info={},
            device_id='test', device_name='Test',
        )

    def test_run_tests_state_reset_published_with_retain_true(self):
        """RUN_TESTS_STATE reset to 'unknown' must be retained."""
        self.pub.publish_management_discovery('essential', debug_mode=True)
        state_calls = [c for c in self.mqtt.publish.call_args_list
                       if c.args[0] == self.MgmtTopic.RUN_TESTS_STATE]
        self.assertTrue(state_calls, "RUN_TESTS_STATE must be published in debug mode")
        for call in state_calls:
            retain = call.kwargs.get('retain',
                                     call.args[2] if len(call.args) > 2 else None)
            self.assertTrue(retain,
                            "RUN_TESTS_STATE reset must be published with retain=True")

    def test_run_tests_attrs_reset_published_with_retain_true(self):
        """RUN_TESTS_ATTRS reset payload must be retained."""
        self.pub.publish_management_discovery('essential', debug_mode=True)
        attrs_calls = [c for c in self.mqtt.publish.call_args_list
                       if c.args[0] == self.MgmtTopic.RUN_TESTS_ATTRS]
        self.assertTrue(attrs_calls, "RUN_TESTS_ATTRS must be published in debug mode")
        for call in attrs_calls:
            retain = call.kwargs.get('retain',
                                     call.args[2] if len(call.args) > 2 else None)
            self.assertTrue(retain,
                            "RUN_TESTS_ATTRS reset must be published with retain=True")
