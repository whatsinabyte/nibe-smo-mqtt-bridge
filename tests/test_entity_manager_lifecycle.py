"""
test_entity_manager_lifecycle.py
================================
Entity enable/disable and mode lifecycle tests for nibe_entity_manager.py — split out of test_entity_manager.py
for file-size/maintainability. Shared fixtures are in conftest.py.
"""

import json
import time
import unittest
from unittest.mock import MagicMock, mock_open, patch

from conftest import (
    _make_em,
    _nibe_point_id,
)
from hypothesis import example, given
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
)


class TestIsSuppressedProperties(unittest.TestCase):
    """Hypothesis properties for EntityManager._is_suppressed."""

    @given(st.integers(min_value=1, max_value=100))
    def test_positive_depth_returns_true(self, depth):
        """Any positive suppression depth must return True."""
        em = _make_em()
        em._suppress_enabled_state_depth = depth
        self.assertTrue(em._is_suppressed())

    def test_zero_depth_returns_false(self):
        """Zero depth must always return False."""
        em = _make_em()
        em._suppress_enabled_state_depth = 0
        self.assertFalse(em._is_suppressed())

    @given(st.integers(min_value=1, max_value=100))
    def test_always_returns_bool(self, depth):
        em = _make_em()
        em._suppress_enabled_state_depth = depth
        self.assertIsInstance(em._is_suppressed(), bool)

    def test_initial_state_not_suppressed(self):
        """Fresh EntityManager must not be suppressed."""
        em = _make_em()
        self.assertFalse(em._is_suppressed())


class TestEnableDisableEntity(unittest.TestCase):
    """Tests for EntityManager.enable_entity() and disable_entity()."""

    def setUp(self):
        self.em = _make_em()
        self.point_id = 4

        # Seed a fully-specified indexed point
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
        self.em.bulk_data[self.point_id] = {
            'raw_value': 119, 'string_value': '', 'is_ok': True,
            'metadata': {'divisor': 10}, 'title': 'Outdoor temperature',
            'description': '', 'timestamp': time.time(),
        }

        # Make publish_entity_discovery return a realistic entity_info dict
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

    # ── enable_entity ─────────────────────────────────────────────────────────

    def test_enable_unknown_point_returns_false(self):
        result = self.em.enable_entity(99999)
        self.assertFalse(result)

    def test_enable_adds_to_mqtt_enabled_points(self):
        self.em.enable_entity(self.point_id)
        self.assertIn(self.point_id, self.em.mqtt_enabled_points)

    def test_enable_adds_to_active_entities(self):
        self.em.enable_entity(self.point_id)
        with self.em._active_entities_lock:
            self.assertIn(self.point_id, self.em.active_entities_by_id)

    def test_enable_calls_publish_entity_discovery(self):
        self.em.enable_entity(self.point_id)
        self.em._pub.publish_entity_discovery.assert_called_once_with(
            self.em.all_points_by_id[self.point_id], self.em.bulk_data
        )

    def test_enable_publishes_availability_online(self):
        self.em.enable_entity(self.point_id)
        calls = [str(c) for c in self.em.mqtt.publish.call_args_list]
        avail_calls = [c for c in calls if 'availability' in c and 'online' in c]
        self.assertTrue(len(avail_calls) > 0, "Availability 'online' should be published")

    def test_enable_returns_true_on_success(self):
        result = self.em.enable_entity(self.point_id)
        self.assertTrue(result)

    def test_enable_already_enabled_returns_true_without_republish(self):
        self.em.enable_entity(self.point_id)
        publish_count_after_first = self.em.mqtt.publish.call_count
        result = self.em.enable_entity(self.point_id)
        self.assertTrue(result)
        # No additional publish calls should have happened
        self.assertEqual(self.em.mqtt.publish.call_count, publish_count_after_first)

    def test_enable_increments_type_stats(self):
        self.em.enable_entity(self.point_id)
        self.assertIn('sensor', self.em._stats_type_counts)
        self.assertGreater(self.em._stats_type_counts['sensor'], 0)

    def test_enable_writable_increments_writable_count(self):
        # Override with a writable point
        self.em.all_points_by_id[self.point_id]['is_writable'] = True
        before = self.em._stats_writable_count
        self.em.enable_entity(self.point_id)
        self.assertEqual(self.em._stats_writable_count, before + 1)

    def test_increment_stats_accumulates_writable_count_not_resets_to_one(self):
        """_stats_writable_count must accumulate (+=1), not be reset to 1 —
        a starting count of 0 can't distinguish the two, so this seeds a
        nonzero count first."""
        self.em._stats_writable_count = 5
        point = {'entity_type': 'switch', 'entity_category': 'config', 'is_writable': True}
        self.em._increment_stats(point)
        self.assertEqual(self.em._stats_writable_count, 6)

    def test_enable_publish_fails_returns_false(self):
        """If discovery publish fails (returns None), enable must return False."""
        self.em._pub.publish_entity_discovery.return_value = None
        result = self.em.enable_entity(self.point_id)
        self.assertFalse(result)
        self.assertNotIn(self.point_id, self.em.mqtt_enabled_points)

    def test_enable_read_only_sensor_does_not_subscribe_command_topic(self):
        """A read-only sensor (command_topic=None) must not subscribe to MQTT."""
        self.em.enable_entity(self.point_id)
        subscribe_calls = [str(c) for c in self.em.mqtt.subscribe.call_args_list]
        command_subs = [c for c in subscribe_calls if 'command' in c.lower()]
        self.assertEqual(len(command_subs), 0)

    def test_writable_entity_command_callback_dispatches_to_handle_command(self):
        """When a writable entity is enabled, the registered MQTT command callback
        must invoke _handle_command (line 863)."""
        cmd_topic = f'homeassistant/switch/nibe_{self.point_id}/set'
        self.mock_entity_info['command_topic'] = cmd_topic
        self.mock_entity_info['entity_type'] = 'switch'

        stored_cb = {}
        def fake_callback_add(topic, cb):
            stored_cb[topic] = cb
        self.em.mqtt.message_callback_add = MagicMock(side_effect=fake_callback_add)

        self.em.enable_entity(self.point_id)
        self.assertIn(cmd_topic, stored_cb)

        msg = MagicMock()
        msg.payload = b'1'
        with patch.object(self.em, '_handle_command') as mock_handle:
            stored_cb[cmd_topic](None, None, msg)
        mock_handle.assert_called_once_with(self.mock_entity_info, msg)

    # ── disable_entity ────────────────────────────────────────────────────────

    def test_disable_not_enabled_returns_true(self):
        """Disabling a point that was never enabled is a no-op that returns True."""
        result = self.em.disable_entity(99999)
        self.assertTrue(result)

    def test_disable_removes_from_mqtt_enabled(self):
        self.em.enable_entity(self.point_id)
        self.em.disable_entity(self.point_id)
        self.assertNotIn(self.point_id, self.em.mqtt_enabled_points)

    def test_disable_removes_from_active_entities(self):
        self.em.enable_entity(self.point_id)
        self.em.disable_entity(self.point_id)
        with self.em._active_entities_lock:
            self.assertNotIn(self.point_id, self.em.active_entities_by_id)

    def test_disable_removes_active_dynamic_point_and_persists(self):
        """Regression: manually disabling an active dynamic entity (e.g. via
        the Entity Manager card) must remove it from active_dynamic_points
        and persist that, not just leave it there. active_dynamic_points is
        meant to be "firmware-state-driven, not mode-driven" per apply_mode's
        docstring — apply_mode deliberately protects active dynamic points
        from being disabled by a mode change — but a MANUAL disable is
        explicit user intent, not a mode change, and must override that
        firmware-driven bookkeeping. Without this, _reconcile_dynamic_points()
        silently re-enables the point on the next restart (its controlling
        switch hasn't changed, so it's still "expected active"), undoing the
        user's explicit choice with no indication why."""
        self.em.enable_entity(self.point_id)
        self.em.active_dynamic_points.add(self.point_id)
        self.em.disable_entity(self.point_id)
        self.assertNotIn(self.point_id, self.em.active_dynamic_points)
        # Must be persisted (retained MQTT publish), not just an in-memory
        # change that's lost on the very restart this exists to survive.
        from nibe_mqtt_publisher import BrowserTopic
        persist_calls = [
            c for c in self.em.mqtt.publish.call_args_list
            if c.args[0] == BrowserTopic.ACTIVE_DYNAMIC
        ]
        self.assertTrue(persist_calls, "active_dynamic_points removal must be persisted")

    def test_disable_non_dynamic_point_does_not_touch_active_dynamic_points(self):
        """A disable for a point that was never in active_dynamic_points
        must not publish a spurious ACTIVE_DYNAMIC persist — only an actual
        removal should trigger it."""
        self.em.enable_entity(self.point_id)
        self.em.disable_entity(self.point_id)
        from nibe_mqtt_publisher import BrowserTopic
        persist_calls = [
            c for c in self.em.mqtt.publish.call_args_list
            if c.args[0] == BrowserTopic.ACTIVE_DYNAMIC
        ]
        self.assertFalse(persist_calls)

    def test_disable_clears_last_state(self):
        self.em.enable_entity(self.point_id)
        self.em.last_states[self.point_id] = "11.9"
        self.em.disable_entity(self.point_id)
        self.assertNotIn(self.point_id, self.em.last_states)

    def test_disable_discards_value_cache(self):
        self.em.enable_entity(self.point_id)
        self.em.value_cache.should_publish(self.point_id, 100, threshold=1)
        self.em.disable_entity(self.point_id)
        # After discard, next publish call for this point is treated as first
        self.assertTrue(
            self.em.value_cache.should_publish(self.point_id, 100, threshold=1)
        )

    def test_disable_clears_pending_write(self):
        """A write triggered just before disable must not leak in
        pending_writes forever — nothing else clears it for a point that's
        no longer enabled/polled."""
        self.em.enable_entity(self.point_id)
        with self.em._pending_writes_lock:
            self.em.pending_writes[self.point_id] = {
                'value': 100, 'timestamp': time.time(),
            }
        self.em.disable_entity(self.point_id)
        self.assertNotIn(self.point_id, self.em.pending_writes)

    def test_disable_clears_correct_discovery_config_topic(self):
        """disable_entity must publish an empty retained payload to the
        SPECIFIC entity's own config topic (built from its real entity_type
        and entity_id) — not just publish something. A wrong/None topic
        here would leave a stale discovery config in HA (a ghost entity),
        the same bug class fixed repeatedly elsewhere in this project."""
        from nibe_mqtt_publisher import t_config
        self.em.enable_entity(self.point_id)
        self.em.mqtt.reset_mock()
        self.em.disable_entity(self.point_id)
        expected_topic = t_config('sensor', f'nibe_{self.point_id}')
        clear_calls = [
            c for c in self.em.mqtt.publish.call_args_list
            if c.args[0] == expected_topic and c.args[1] == ""
        ]
        self.assertTrue(
            clear_calls,
            f"Expected an empty-payload publish to {expected_topic!r}, "
            f"got calls: {self.em.mqtt.publish.call_args_list}"
        )

    def test_disable_decrements_type_stats(self):
        self.em.enable_entity(self.point_id)
        count_before = self.em._stats_type_counts.get('sensor', 0)
        self.em.disable_entity(self.point_id)
        count_after = self.em._stats_type_counts.get('sensor', 0)
        self.assertEqual(count_after, count_before - 1)

    def test_disable_stat_count_never_below_zero(self):
        """Stats decrement must be guarded against going negative."""
        self.em._stats_type_counts['sensor'] = 0
        self.em.all_points_by_id[self.point_id]['entity_type'] = 'sensor'
        self.em.mqtt_enabled_points.add(self.point_id)
        with self.em._active_entities_lock:
            self.em.active_entities_by_id[self.point_id] = self.mock_entity_info
        self.em.disable_entity(self.point_id)
        self.assertGreaterEqual(self.em._stats_type_counts.get('sensor', 0), 0)

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

    def test_disable_pops_caches_without_default_when_absent(self):
        """A point never seen by _point_string_cache/_entity_type_cache
        must still disable cleanly — a missing .pop() default there would
        raise KeyError instead of silently no-op'ing."""
        self.em.enable_entity(self.point_id)
        # Deliberately never populate _point_string_cache/_entity_type_cache
        # for this point_id, unlike test_disable_pops_caches_keyed_by_this_point_id.
        self.em.disable_entity(self.point_id)  # must not raise

    def test_disable_clears_missing_state_topic_warned_for_this_point(self):
        """A point previously warned about a missing state_topic must have
        that dedup marker cleared on disable — otherwise re-enabling it
        later would silently suppress the warning it should get again."""
        self.em.enable_entity(self.point_id)
        self.em._missing_state_topic_warned.add(self.point_id)
        self.em.disable_entity(self.point_id)
        self.assertNotIn(self.point_id, self.em._missing_state_topic_warned)

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

    def test_enable_then_disable_round_trip(self):
        """Full enable→disable cycle leaves the entity manager in a clean state."""
        self.em.enable_entity(self.point_id)
        self.assertIn(self.point_id, self.em.mqtt_enabled_points)
        self.em.disable_entity(self.point_id)
        self.assertNotIn(self.point_id, self.em.mqtt_enabled_points)
        with self.em._active_entities_lock:
            self.assertNotIn(self.point_id, self.em.active_entities_by_id)
        self.assertNotIn(self.point_id, self.em.last_states)

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


class TestEnableDisableEntityProperties(unittest.TestCase):
    """Hypothesis properties for enable_entity/disable_entity."""

    def _seeded_em(self, pid):
        em = _make_em()
        em.all_points_by_id[pid] = {
            'variableId':    pid,
            'display_title': f'Point {pid}',
            'entity_type':   'sensor',
            'entity_category': 'diagnostic',
            'is_writable':   False,
            'is_dynamic':    False,
            'description':   '',
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
        em.bulk_data[pid] = {'raw_value': 0, 'is_ok': True}
        return em

    @given(_nibe_point_id.filter(lambda p: p > 0))
    def test_mqtt_enabled_points_grows_on_enable(self, pid):
        """After enable, pid must appear in mqtt_enabled_points."""
        em = self._seeded_em(pid)
        em.enable_entity(pid)
        self.assertIn(pid, em.mqtt_enabled_points)

    @given(_nibe_point_id.filter(lambda p: p > 0))
    def test_enable_then_disable_removes_from_enabled(self, pid):
        """enable followed by disable must remove pid from mqtt_enabled_points."""
        em = self._seeded_em(pid)
        em.enable_entity(pid)
        em.disable_entity(pid)
        self.assertNotIn(pid, em.mqtt_enabled_points)

    @given(_nibe_point_id.filter(lambda p: p > 0))
    def test_enable_twice_is_idempotent(self, pid):
        """Enabling an already-enabled point must not duplicate the entry."""
        em = self._seeded_em(pid)
        em.enable_entity(pid)
        count_first = em.mqtt_enabled_points.count(pid) \
            if hasattr(em.mqtt_enabled_points, 'count') \
            else (1 if pid in em.mqtt_enabled_points else 0)
        em.enable_entity(pid)
        count_second = em.mqtt_enabled_points.count(pid) \
            if hasattr(em.mqtt_enabled_points, 'count') \
            else (1 if pid in em.mqtt_enabled_points else 0)
        self.assertEqual(count_first, count_second)

    @given(_nibe_point_id.filter(lambda p: p > 0))
    def test_disable_never_raises(self, pid):
        """Disabling a point never raises regardless of initial state."""
        em = self._seeded_em(pid)
        em.disable_entity(pid)  # must not raise even if not enabled


class EntityManagerMachine(RuleBasedStateMachine):
    """Stateful test machine for EntityManager enable/disable/write/changelog.

    Hypothesis generates arbitrary sequences of operations and checks that
    invariants hold after every step.
    """

    # ── Setup ────────────────────────────────────────────────────────────────

    @initialize()
    def setup(self):
        self.em = _make_em()
        self._initial_seq = self.em._history_seq
        # Pre-populate bulk_data and all_points_by_id for a few known pids
        # so enable_entity has valid points to work with.
        self._known_pids = [100, 200, 300, 400, 500]
        for pid in self._known_pids:
            self.em.all_points_by_id[pid] = {
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
            self.em.bulk_data[pid] = {'raw_value': 0, 'is_ok': True}

    # ── Rules (operations) ───────────────────────────────────────────────────

    @rule(pid=st.sampled_from([100, 200, 300, 400, 500]))
    def enable(self, pid):
        self.em.enable_entity(pid)

    @rule(pid=st.sampled_from([100, 200, 300, 400, 500]))
    def disable(self, pid):
        self.em.disable_entity(pid)

    @rule(pid=st.sampled_from([100, 200, 300, 400, 500]),
          value=st.integers(min_value=0, max_value=100))
    def add_pending_write(self, pid, value):
        """Simulate a pending write entry as the write executor would create it."""
        import time as _time
        self.em.pending_writes[pid] = {
            'value': value,
            'time': _time.time(),
            'entity_id': f'sensor.nibe_{pid}',
        }

    @rule()
    def evict_stale_writes(self):
        """Evict all pending writes older than _STALE_WRITE_AGE_S."""
        import time as _time

        from nibe_entity_manager import _STALE_WRITE_AGE_S
        now = _time.time()
        stale = [p for p, v in self.em.pending_writes.items()
                 if now - v['time'] > _STALE_WRITE_AGE_S]
        for p in stale:
            self.em.pending_writes.pop(p, None)

    @rule(added=st.lists(st.integers(min_value=1, max_value=9999), max_size=3),
          removed=st.lists(st.integers(min_value=1, max_value=9999), max_size=3))
    def add_changelog_entry(self, added, removed):
        import time as _time
        self.em._last_prune_time = _time.time()  # suppress pruning during test
        # Production code stores dicts with id/title/type keys, not raw ints.
        # Using production-shaped data so the changelog_added_removed_are_lists
        # invariant and any downstream rendering code sees the correct structure.
        added_dicts   = [{'id': p, 'title': f'Point {p}', 'type': 'sensor'}
                         for p in added]
        removed_dicts = [{'id': p, 'title': f'Point {p}', 'type': 'sensor'}
                         for p in removed]
        self.em._update_changelog_history({
            'added': added_dicts, 'removed': removed_dicts, 'source': 'test',
        })

    @rule()
    def mark_changelog_read(self):
        self.em.mark_changelog_read()

    @rule(pid=st.sampled_from([100, 200, 300, 400, 500]))
    def update_bulk_value(self, pid):
        """Simulate a firmware poll updating a point's value."""
        self.em.bulk_data[pid] = {'raw_value': 42, 'is_ok': True}

    @rule(pid=st.sampled_from([100, 200, 300, 400, 500]))
    def clear_bulk_value(self, pid):
        """Simulate a point disappearing from bulk data (dynamic point gone)."""
        self.em.bulk_data.pop(pid, None)

    # ── Invariants (checked after every rule) ────────────────────────────────

    @invariant()
    def active_entities_list_matches_dict(self):
        """len(active_entities) always equals len(active_entities_by_id)."""
        assert len(self.em.active_entities) == len(self.em.active_entities_by_id), (
            f"active_entities list ({len(self.em.active_entities)}) "
            f"!= active_entities_by_id dict ({len(self.em.active_entities_by_id)})"
        )

    @invariant()
    def active_entities_subset_of_enabled(self):
        """active_entities_by_id.keys() must always be ⊆ mqtt_enabled_points."""
        active_pids = set(self.em.active_entities_by_id.keys())
        enabled_pids = set(self.em.mqtt_enabled_points)
        extra = active_pids - enabled_pids
        assert not extra, (
            f"Points in active_entities_by_id but NOT in mqtt_enabled_points: {extra}"
        )

    @invariant()
    def enabled_count_non_negative(self):
        assert len(self.em.mqtt_enabled_points) >= 0

    @invariant()
    def pending_writes_well_formed(self):
        """Every pending write entry must have 'value' and 'time' keys."""
        for pid, entry in self.em.pending_writes.items():
            assert 'value' in entry, f"pending_writes[{pid}] missing 'value'"
            assert 'time' in entry, f"pending_writes[{pid}] missing 'time'"

    @invariant()
    def history_seq_never_decreases(self):
        assert self.em._history_seq >= self._initial_seq, (
            f"_history_seq decreased: {self.em._history_seq} < {self._initial_seq}"
        )
        self._initial_seq = self.em._history_seq  # ratchet forward

    @invariant()
    def last_published_seq_leq_history_seq(self):
        assert self.em._last_published_seq <= self.em._history_seq, (
            f"_last_published_seq ({self.em._last_published_seq}) "
            f"> _history_seq ({self.em._history_seq})"
        )

    @invariant()
    def changelog_entries_have_required_keys(self):
        """Every changelog entry must have the required structural keys."""
        required = {'id', 'timestamp', 'unread', 'added', 'removed'}
        for entry in self.em.change_history:
            missing = required - set(entry.keys())
            assert not missing, f"Changelog entry missing keys: {missing}"


# pytest discovers RuleBasedStateMachine via TestCase subclassing
    @rule()
    def suppress_enabled_state(self):
        """Increment the suppression depth counter."""
        self.em._suppress_enabled_state_depth += 1

    @rule()
    def unsuppress_enabled_state(self):
        """Decrement the suppression depth counter — never below zero."""
        if self.em._suppress_enabled_state_depth > 0:
            self.em._suppress_enabled_state_depth -= 1

    @rule(pid=st.sampled_from([100, 200, 300, 400, 500]))
    def invalidate_config_hash(self, pid):
        self.em._pub.invalidate_config_hash(pid)

    @invariant()
    def suppression_depth_non_negative(self):
        assert self.em._suppress_enabled_state_depth >= 0, (
            f"_suppress_enabled_state_depth went negative: "
            f"{self.em._suppress_enabled_state_depth}"
        )

    @invariant()
    def changelog_within_maxlen(self):
        assert len(self.em.change_history) <= self.em.change_history.maxlen, (
            f"change_history exceeded maxlen: "
            f"{len(self.em.change_history)} > {self.em.change_history.maxlen}"
        )

    @invariant()
    def changelog_entry_ids_well_formed(self):
        for entry in self.em.change_history:
            assert entry['id'].startswith('change_'), (
                f"Changelog entry id malformed: {entry['id']!r}"
            )

    @invariant()
    def mqtt_enabled_points_is_set(self):
        """mqtt_enabled_points must be a set — no duplicates."""
        assert isinstance(self.em.mqtt_enabled_points, set), (
            f"mqtt_enabled_points is {type(self.em.mqtt_enabled_points).__name__}"
        )

    @rule(pid=st.sampled_from([100, 200, 300, 400, 500]),
          pending_value=st.integers(min_value=0, max_value=100),
          bulk_value=st.integers(min_value=0, max_value=100))
    def pending_write_suppresses_state_publish(self, pid, pending_value, bulk_value):
        """While a pending write exists and bulk value differs from written value,
        _update_entity_state must not publish to the state topic."""
        import time as _time
        if pending_value == bulk_value:
            return
        self.em.pending_writes[pid] = {
            'value': pending_value, 'timestamp': _time.time(),
            'time': _time.time(), 'cmd_id': 'test',
        }
        self.em.bulk_data[pid] = {
            'raw_value': bulk_value, 'is_ok': True, 'string_value': '',
            'metadata': {'variableSize': 'u8', 'divisor': 1,
                         'unit': '', 'change': 0, 'decimal': 0},
            'title': f'Point {pid}',
        }
        entity_info = {
            'point_id': pid, 'entity_type': 'sensor',
            'availability_topic': f'nibe/avail/{pid}',
            'state_topic': f'nibe/state/{pid}',
            'command_topic': None, 'point_data': {},
        }
        self.em.active_entities_by_id[pid] = entity_info
        self.em.mqtt_enabled_points.add(pid)
        before = list(self.em.mqtt.publish.call_args_list)
        self.em._update_entity_state(entity_info)
        after = list(self.em.mqtt.publish.call_args_list)
        state_publishes = [c for c in after[len(before):]
                           if c.args[0] == f'nibe/state/{pid}']
        assert not state_publishes, (
            f"Published to nibe/state/{pid} while pending write active"
        )
        self.em.pending_writes.pop(pid, None)
        self.em.active_entities_by_id.pop(pid, None)
        self.em.mqtt_enabled_points.discard(pid)


    @rule(mode=st.sampled_from(['none', 'all']))
    def apply_mode(self, mode):
        """apply_mode reconciles mqtt_enabled_points to the target mode.

        'none' disables all points; 'all' enables all known points.
        Both work regardless of which pids are in MODES frozensets since
        'none' → frozenset() and 'all' → set(all_points_by_id.keys()).
        This exercises the suppress/unsuppress lock, the enable/disable
        loops, and the persist call — all in a single state transition."""
        self.em.apply_mode(mode)

    @rule(
        subset=st.lists(
            st.sampled_from([100, 200, 300, 400, 500]), min_size=0, max_size=5, unique=True,
        ),
    )
    def apply_named_mode(self, subset):
        """The apply_mode rule above only ever passes 'none'/'all', which
        take the two special-cased branches (empty frozenset / None-sentinel
        — nibe_entity_manager.py:946-950). A real named mode (e.g.
        'essential') takes the third, previously-untested branch: mode_value
        = MODES.get(mode_name) resolves to an actual frozenset, and target =
        set(mode_value) is used directly. Patch a throwaway named mode into
        MODES pointing at an arbitrary subset of known pids — mirroring the
        established pattern this codebase already uses for testing named
        modes (nibe_entity_detection.py:65: 'Tests that need a concrete set
        patch MODES[...] directly') — to exercise that branch for real."""
        from nibe_entity_manager import MODES
        known = {100, 200, 300, 400, 500}
        before_enabled = set(self.em.mqtt_enabled_points) & known
        protected = set(self.em.active_dynamic_points) & known
        with patch.dict(MODES, {'__test_named_mode__': frozenset(subset)}):
            self.em.apply_mode('__test_named_mode__')
        after_enabled = set(self.em.mqtt_enabled_points) & known
        # Points in the mode's target set get enabled; points previously
        # enabled but not in the target only survive if they're an active
        # dynamic point (protected from mode-driven disable).
        expected = set(subset) | (before_enabled & protected)
        assert after_enabled == expected, (
            f"apply_mode(named mode -> {set(subset)}) left {after_enabled} "
            f"enabled among known pids, expected {expected} "
            f"(before={before_enabled}, protected={protected})"
        )

    @rule(pid=st.sampled_from([100, 200, 300, 400, 500]),
          entity_type=st.sampled_from(['switch', 'number', 'sensor', 'select']),
          raw_value=st.integers(min_value=0, max_value=10))
    def update_entity_state_writable(self, pid, entity_type, raw_value):
        """Exercise _update_entity_state for writable entity types (switch,
        number, select) — the existing machine only uses 'sensor'. Writable
        types have different command_topic and value-mapping paths."""
        self.em.bulk_data[pid] = {
            'raw_value': raw_value, 'is_ok': True, 'string_value': '',
            'metadata': {
                'variableSize': 'u8', 'divisor': 1, 'unit': '',
                'change': 0, 'decimal': 0,
                'minValue': 0, 'maxValue': 10,
                'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
            },
            'title': f'Point {pid}',
        }
        entity_info = {
            'point_id':            pid,
            'entity_type':         entity_type,
            'availability_topic':  f'nibe/avail/{pid}',
            'state_topic':         f'nibe/state/{pid}',
            'command_topic':       f'homeassistant/{entity_type}/nibe_{pid}/set',
            'point_data':          {},
        }
        self.em.active_entities_by_id[pid] = entity_info
        self.em.mqtt_enabled_points.add(pid)
        self.em._update_entity_state(entity_info)
        self.em.active_entities_by_id.pop(pid, None)
        self.em.mqtt_enabled_points.discard(pid)

    # ── Discovery / bootstrap lifecycle ─────────────────────────────────────
    # discover_points, complete_deferred_discovery, scan_mqtt_discovery, and
    # restore_from_mqtt were previously never called by this machine at all —
    # entities only ever appeared via the synthetic setup() seeding, so
    # Hypothesis never explored these interleaved with enable/disable/write/
    # mode-change sequences. Each rule below stubs out only the pieces that
    # already have independent dedicated coverage elsewhere (_fetch_bulk_data,
    # the sub-steps of complete_deferred_discovery — see
    # TestCompleteDeferredDiscovery / TestScanMqttDiscovery in
    # test_entity_manager_discovery.py for the established mocking pattern
    # this mirrors), using unittest.mock.patch.object scoped to just the rule
    # body so the stubs never leak into other rules' calls to the same
    # methods later in the same run.

    @rule()
    def discover_points(self):
        """Exercise discover_points' own bootstrap logic (baseline
        establishment, DynamicPointMap population) — not _fetch_bulk_data
        itself, which is the ~297-line function this project deliberately
        does not refactor and which has its own dedicated test coverage."""
        with patch.object(self.em, '_fetch_bulk_data', return_value=True):
            result = self.em.discover_points()
        assert result is True
        assert self.em.initial_discovery_complete is True
        assert self.em.baseline_point_ids == set(self._known_pids)

    @rule(
        applied_mode=st.sampled_from([None, 'none', 'essential', 'all']),
        mqtt_enabled_count=st.integers(min_value=0, max_value=3),
    )
    def complete_deferred_discovery(self, applied_mode, mqtt_enabled_count):
        """Exercise the apply/restore/reconcile three-way branch
        (decide_startup_action, nibe_entity_manager.py:172) that
        complete_deferred_discovery drives — real production logic, not
        stubbed. Its sub-steps (discover_points, scan_mqtt_discovery,
        restore_from_mqtt, apply_mode, record_applied_mode,
        publish_enabled_state) are stubbed exactly as
        TestCompleteDeferredDiscovery does, since each already has its own
        dedicated test coverage; this rule targets the branch-selection
        logic itself, exercised alongside arbitrary enable/disable/write
        state."""
        discovered = set(range(mqtt_enabled_count))
        config_mode = 'essential'
        with patch.object(self.em, 'discover_points', return_value=True), \
             patch.object(self.em, 'scan_mqtt_discovery', return_value=discovered), \
             patch.object(self.em, 'read_applied_mode', return_value=applied_mode), \
             patch.object(self.em, 'restore_from_mqtt') as mock_restore, \
             patch.object(self.em, 'apply_mode') as mock_apply, \
             patch.object(self.em, 'record_applied_mode') as mock_record, \
             patch.object(self.em, 'publish_enabled_state'), \
             patch.object(self.em._api, 'fetch_device_info', return_value={
                 'serial': '1', 'firmware': '1', 'model': 'S',
             }):
            result = self.em.complete_deferred_discovery(config_mode)

        assert result is True
        if mqtt_enabled_count == 0:
            # apply: fresh install
            mock_apply.assert_called_once_with(config_mode)
            mock_restore.assert_not_called()
            mock_record.assert_not_called()
        elif applied_mode is None or applied_mode == config_mode:
            # restore: same mode, or migration boundary
            mock_restore.assert_called_once()
            mock_apply.assert_not_called()
            if applied_mode is None:
                mock_record.assert_called_once_with(config_mode)
            else:
                mock_record.assert_not_called()
        else:
            # reconcile: deliberate mode change detected across a restart
            mock_restore.assert_called_once()
            mock_apply.assert_called_once_with(config_mode)
            mock_record.assert_not_called()

    @rule(
        retained_pids=st.lists(
            st.sampled_from([100, 200, 300, 400, 500]), min_size=0, max_size=3, unique=True,
        ),
    )
    def scan_mqtt_discovery(self, retained_pids):
        """Exercise the retained-config scan (nibe_entity_manager.py:641) —
        wires mqtt.message_callback_add/publish so the sentinel fires
        synchronously, the same pattern TestScanMqttDiscovery uses, avoiding
        the real 15s sentinel timeout (_MQTT_SCAN_TIMEOUT_S)."""
        callbacks = {}

        def fake_callback_add(topic, cb):
            callbacks[topic] = cb

        def fake_publish(topic, _payload, retain=False):
            if 'scan_sentinel' in str(topic):
                import json as _json
                for pid in retained_pids:
                    msg = MagicMock()
                    msg.topic = f'homeassistant/sensor/nibe_{pid}/config'
                    msg.payload = _json.dumps({'unique_id': f'nibe_{pid}'}).encode()
                    cb = callbacks.get('homeassistant/+/+/config')
                    if cb:
                        cb(None, None, msg)
                cb = callbacks.get(topic)
                if cb:
                    cb(None, None, MagicMock())

        with patch.object(self.em.mqtt, 'message_callback_add', side_effect=fake_callback_add), \
             patch.object(self.em.mqtt, 'publish', side_effect=fake_publish):
            result = self.em.scan_mqtt_discovery()

        assert result == set(retained_pids), (
            f"scan_mqtt_discovery() = {result}, expected {set(retained_pids)}"
        )
        assert self.em.mqtt_enabled_points == set(retained_pids), (
            "scan_mqtt_discovery must replace (not merge into) mqtt_enabled_points"
        )
        # scan_mqtt_discovery wholesale-replaces mqtt_enabled_points and is,
        # in production, only ever called once at startup before
        # enable_entity/restore_from_mqtt/apply_mode populate
        # active_entities_by_id. Prune any active entities this rule's
        # replacement left stranded outside the new enabled set, mirroring
        # what a real startup sequence guarantees, so this rule stays
        # consistent with active_entities_subset_of_enabled when Hypothesis
        # interleaves it with the rest of the machine.
        stale = set(self.em.active_entities_by_id) - self.em.mqtt_enabled_points
        for pid in stale:
            self.em.active_entities_by_id.pop(pid, None)

    @rule(
        pids=st.lists(
            st.sampled_from([100, 200, 300, 400, 500]), min_size=0, max_size=3, unique=True,
        ),
    )
    def restore_from_mqtt(self, pids):
        """Exercise rebuilding active_entities_by_id from mqtt_enabled_points
        (nibe_entity_manager.py:700). Grows (never replaces)
        mqtt_enabled_points, matching restore_from_mqtt's own real contract:
        it only ever removes an entry on failure (no metadata), which cannot
        happen here since every sampled pid has metadata from setup()."""
        self.em.mqtt_enabled_points |= set(pids)
        # restore_from_mqtt restores every pid currently in mqtt_enabled_points,
        # not just the ones this call added — earlier rules (enable, apply_mode)
        # may already have populated it, so the expected restore count is the
        # full current set, not just len(pids).
        expected_restored = len(self.em.mqtt_enabled_points)

        def fake_publish_discovery(point, _bulk_data):
            pid = point['variableId']
            # Shape matches real publish_entity_discovery output closely
            # enough for apply_mode's later _disable_entity_locked call
            # (nibe_entity_manager.py:859) to succeed on this entity.
            return {
                'point_id':           pid,
                'entity_id':          f'sensor.nibe_{pid}',
                'entity_type':        'sensor',
                'command_topic':      None,
                'availability_topic': f'nibe/avail/{pid}',
                'state_topic':        f'nibe/state/{pid}',
                'is_dynamic':         False,
            }

        with patch.object(self.em._pub, 'publish_entity_discovery',
                           side_effect=fake_publish_discovery):
            restored = self.em.restore_from_mqtt()

        assert restored == expected_restored, (
            f"restore_from_mqtt() restored {restored}, expected "
            f"{expected_restored} (all known pids have metadata from setup())"
        )
        for pid in pids:
            assert pid in self.em.active_entities_by_id, (
                f"restore_from_mqtt() did not populate active_entities_by_id[{pid}]"
            )

    @invariant()
    def active_dynamic_points_subset_of_mqtt_enabled(self):
        """Every point in active_dynamic_points must also be in mqtt_enabled_points
        once it has been indexed — active but not enabled is an inconsistent state."""
        # Only check points that are in active_entities_by_id (fully indexed);
        # active_dynamic_points can transiently lead mqtt_enabled_points during
        # the reconcile window, so we guard on full indexing.
        indexed = set(self.em.active_entities_by_id.keys())
        active_and_indexed = self.em.active_dynamic_points & indexed
        not_enabled = active_and_indexed - self.em.mqtt_enabled_points
        assert not not_enabled, (
            f"Active dynamic points indexed but not in mqtt_enabled_points: "
            f"{not_enabled}"
        )

    @invariant()
    def changelog_added_removed_are_lists(self):
        """Every changelog entry's 'added' and 'removed' fields must be lists."""
        for entry in self.em.change_history:
            assert isinstance(entry.get('added'), list), (
                f"Changelog 'added' is not a list: {type(entry.get('added'))}"
            )
            assert isinstance(entry.get('removed'), list), (
                f"Changelog 'removed' is not a list: {type(entry.get('removed'))}"
            )

    @invariant()
    def apply_mode_none_leaves_no_enabled_static(self):
        """After apply_mode('none'), only active dynamic points remain enabled.
        This is checked only when the mode has been applied — we probe the
        current enabled set for any non-dynamic members, which would indicate
        the suppression or disable loop had a bug."""
        # This invariant cannot know whether apply_mode('none') was the last
        # operation, so we verify the weaker property that is always true:
        # mqtt_enabled_points ⊇ active_dynamic_points (dynamic points are
        # never disabled by apply_mode regardless of mode).
        for pid in self.em.active_dynamic_points:
            if pid in self.em.active_entities_by_id:
                assert pid in self.em.mqtt_enabled_points, (
                    f"Active indexed dynamic point {pid} not in mqtt_enabled_points"
                )


EntityManagerStatefulTest = EntityManagerMachine.TestCase


class TestSuppressEnabledState(unittest.TestCase):

    def test_depth_increments_inside_context(self):
        em = _make_em()
        self.assertEqual(em._suppress_enabled_state_depth, 0)
        with em._suppress_enabled_state():
            self.assertEqual(em._suppress_enabled_state_depth, 1)
        self.assertEqual(em._suppress_enabled_state_depth, 0)

    def test_nested_contexts_increment_depth(self):
        em = _make_em()
        with em._suppress_enabled_state():
            with em._suppress_enabled_state():
                self.assertEqual(em._suppress_enabled_state_depth, 2)
            self.assertEqual(em._suppress_enabled_state_depth, 1)
        self.assertEqual(em._suppress_enabled_state_depth, 0)

    def test_is_suppressed_returns_true_inside(self):
        em = _make_em()
        with em._suppress_enabled_state():
            self.assertTrue(em._suppress_enabled_state_depth > 0)

    def test_depth_restored_after_exception(self):
        em = _make_em()
        try:
            with em._suppress_enabled_state():
                raise ValueError("test")
        except ValueError:
            pass
        self.assertEqual(em._suppress_enabled_state_depth, 0)


class TestHaDisableNotifIdProperties(unittest.TestCase):
    """ha_disable_notif_id()'s sanitize-and-truncate logic — the earlier
    "two call sites must agree" test only exercises one short, dash-free
    entity_id ('switch.nibe_100'), never the truncation (60-char cap) or
    dash-replacement behavior the function's own docstring promises."""

    @given(st.text(min_size=61, max_size=300))
    def test_result_length_is_bounded_regardless_of_input_length(self, ha_entity_id):
        """The safe_id portion is capped at 60 chars — the full
        notification_id must never exceed that cap plus the fixed
        'nibe_ha_disable_' prefix, no matter how long ha_entity_id is.
        min_size=61 guarantees every generated input is long enough to
        actually exercise the truncation (Hypothesis's default text()
        distribution skews short enough that a broader min_size=1 range
        could pass many runs without ever generating a long string)."""
        em = _make_em()
        result = em.ha_disable_notif_id(ha_entity_id)
        self.assertLessEqual(len(result), len('nibe_ha_disable_') + 60)

    @given(st.text(alphabet=st.characters(blacklist_characters='.-'),
                   min_size=1, max_size=50))
    def test_short_dot_dash_free_id_passes_through_unmodified(self, ha_entity_id):
        """For any entity_id short enough to avoid truncation and already
        free of '.'/'-', the safe_id portion must equal the input exactly
        — no unexpected extra sanitization beyond what the docstring
        promises."""
        em = _make_em()
        result = em.ha_disable_notif_id(ha_entity_id)
        self.assertEqual(result, f'nibe_ha_disable_{ha_entity_id}')

    @given(st.text(min_size=1, max_size=300))
    @example('switch.living_room-thermostat.setpoint-2')  # guarantees both '.' and '-' present
    def test_result_never_contains_a_literal_dot_or_dash(self, ha_entity_id):
        """'.' and '-' are both replaced with '_' — the notification_id is
        used as an HA notification identifier, where these characters are
        used as this function's own domain-separator convention, so any
        that leak through would be a real correctness bug, not cosmetic."""
        em = _make_em()
        result = em.ha_disable_notif_id(ha_entity_id)
        safe_id_part = result[len('nibe_ha_disable_'):]
        self.assertNotIn('.', safe_id_part)
        self.assertNotIn('-', safe_id_part)


class TestBuildDisableNotification(unittest.TestCase):
    """Builds the (title, message, notification_id) tuple shown to the user
    when an entity is disabled/re-enabled via HA's own entity settings
    (not the Nibe Entity Manager card). Zero coverage before this. A bug
    here doesn't crash anything — it just shows a wrong or malformed
    notification, or a notif_id that breaks HA's dedupe/dismiss logic."""

    def test_reenabled_action_returns_reenabled_message(self):
        em = _make_em()
        title, message, _notif_id = em.build_disable_notification(
            3920, 'switch.permit_heating', 're-enabled',
        )
        self.assertIn('re-enabled', title.lower())
        self.assertIn('resume publishing', message)

    def test_disabled_static_point_returns_standard_message(self):
        em = _make_em()
        em.all_points_by_id[3920] = {'display_title': 'Permit heating', 'is_dynamic': False}
        title, message, _notif_id = em.build_disable_notification(
            3920, 'switch.permit_heating', 'disabled',
        )
        self.assertEqual(title, 'Nibe Bridge: Entity disabled in HA')
        self.assertIn('#3920 (Permit heating)', message)
        self.assertIn('Entity Manager card', message)

    def test_disabled_dynamic_point_returns_dynamic_specific_message(self):
        """Dynamic points get a different message explaining they'll
        disappear automatically — must not be conflated with the static
        'use the Entity Manager card' guidance, which doesn't apply to them."""
        em = _make_em()
        em.all_points_by_id[50827] = {'display_title': 'Humidity', 'is_dynamic': True}
        title, message, _notif_id = em.build_disable_notification(
            50827, 'sensor.humidity', 'disabled',
        )
        self.assertEqual(title, 'Nibe Bridge: Dynamic entity disabled in HA')
        self.assertIn('firmware-controlled state change', message)
        self.assertNotIn('Entity Manager card', message)

    def test_disabled_dynamic_point_names_controlling_switch_when_known(self):
        """When the controlling switch/select is known in dynamic_point_map,
        the message must name it instead of the generic explanation."""
        from nibe_dynamic_map import DynamicPointEntry
        em = _make_em()
        em.all_points_by_id[50827] = {'display_title': 'Humidity', 'is_dynamic': True}
        em.dynamic_point_map._table[3920] = DynamicPointEntry(
            point_id=3920, title='Additional heating enable', entity_type='switch',
            dynamic_points_by_value={1: [50827]},
        )
        _title, message, _notif_id = em.build_disable_notification(
            50827, 'sensor.humidity', 'disabled',
        )
        self.assertIn('Additional heating enable', message)
        self.assertIn('#3920', message)

    def test_unknown_point_id_falls_back_to_hash_display(self):
        """The point isn't in all_points_by_id (stale data) — must not
        crash, falls back to a bare '#id' display."""
        em = _make_em()
        _title, message, _notif_id = em.build_disable_notification(
            9999, 'switch.unknown', 'disabled',
        )
        self.assertIn('#9999', message)

    def test_none_point_id_falls_back_to_entity_id_display(self):
        """point_id itself is None (couldn't be resolved at all) — falls
        back to showing the raw HA entity_id instead of '#None'."""
        em = _make_em()
        _title, message, _notif_id = em.build_disable_notification(
            None, 'switch.mystery_entity', 'disabled',
        )
        self.assertIn('switch.mystery_entity', message)
        self.assertNotIn('#None', message)

    def test_unknown_point_uses_static_message_not_dynamic(self):
        """When point_id isn't found in all_points_by_id, is_dynamic must
        default to False (static message), not True — an unknown point
        can't be known to be dynamic. The existing 'mystery entity' test
        checks the entity_id appears in the message but not which message
        VARIANT is used; this pins that specifically."""
        em = _make_em()
        title, message, _notif_id = em.build_disable_notification(
            None, 'switch.mystery_entity', 'disabled',
        )
        self.assertEqual(title, 'Nibe Bridge: Entity disabled in HA')
        self.assertIn('Entity Manager card', message)
        self.assertNotIn('firmware-controlled state change', message)

    def test_notification_id_sanitises_dots_and_hyphens(self):
        """notif_id is used as an HA notification identifier — dots and
        hyphens from the entity_id must be replaced with underscores."""
        em = _make_em()
        _, _, notif_id = em.build_disable_notification(
            3920, 'switch.some-entity.name', 'disabled',
        )
        self.assertNotIn('.', notif_id)
        self.assertNotIn('-', notif_id)
        self.assertTrue(notif_id.startswith('nibe_ha_disable_'))

    def test_notification_id_truncated_to_safe_length(self):
        """A very long entity_id must not produce an unbounded notif_id —
        confirms the [:60] truncation is actually applied."""
        em = _make_em()
        long_id = 'switch.' + 'a' * 200
        _, _, notif_id = em.build_disable_notification(3920, long_id, 'disabled')
        self.assertLessEqual(len(notif_id), len('nibe_ha_disable_') + 60)

    def test_notification_id_distinct_per_entity(self):
        """Two different entities must produce two different notif_ids, so
        HA doesn't conflate or dedupe unrelated disable notifications."""
        em = _make_em()
        _, _, id_a = em.build_disable_notification(1, 'switch.a', 'disabled')
        _, _, id_b = em.build_disable_notification(2, 'switch.b', 'disabled')
        self.assertNotEqual(id_a, id_b)

    def test_display_falls_back_to_generic_point_label_when_no_title(self):
        """A point exists in all_points_by_id but has no display_title key
        — falls back to a generic 'Point N' label rather than crashing or
        showing a blank title."""
        em = _make_em()
        em.all_points_by_id[100] = {'is_dynamic': False}  # no display_title
        _title, message, _notif_id = em.build_disable_notification(
            100, 'switch.foo', 'disabled',
        )
        self.assertIn('Point 100', message)

    def test_is_dynamic_defaults_to_static_when_key_absent(self):
        """point.get('is_dynamic', False) — when the point dict lacks the
        'is_dynamic' key entirely, it must default to False (static
        message), never to True or None. Using a point dict with no
        'is_dynamic' key at all (unlike other tests, which always set it
        explicitly) is required to actually exercise the default value."""
        em = _make_em()
        em.all_points_by_id[100] = {'display_title': 'No Dynamic Key'}  # key absent
        title, message, _notif_id = em.build_disable_notification(
            100, 'switch.foo', 'disabled',
        )
        self.assertEqual(title, 'Nibe Bridge: Entity disabled in HA')
        self.assertIn('Entity Manager card', message)
        self.assertNotIn('firmware-controlled', message)

    def test_reenabled_title_exact_case(self):
        """Pins the exact title string (not just a lowercased substring, as
        the existing 're-enabled' test does) so a pure-case mutation of the
        literal is actually caught."""
        em = _make_em()
        title, _, _ = em.build_disable_notification(
            3920, 'switch.permit_heating', 're-enabled',
        )
        self.assertEqual(title, 'Nibe Bridge: Entity re-enabled in HA')

    def test_notification_id_exact_sanitisation(self):
        """Pins the exact sanitised notif_id for a known entity_id — the
        existing tests only check that '.' and '-' are ABSENT afterward,
        which a mutation replacing '.' with 'XX_XX' (still no bare '.' or
        '-' left) would slip past. Verify the precise expected string."""
        em = _make_em()
        _, _, notif_id = em.build_disable_notification(
            3920, 'switch.some-entity.name', 'disabled',
        )
        self.assertEqual(notif_id, 'nibe_ha_disable_switch_some_entity_name')

    def test_dynamic_point_with_known_controller_exact_message(self):
        """Pins the exact wording of the dynamic-with-known-controller
        message body — existing tests only check that the controller's
        title and point_id substrings appear, which doesn't catch
        mutations to the surrounding fixed text ('It will disappear
        automatically...')."""
        from nibe_dynamic_map import DynamicPointEntry
        em = _make_em()
        em.all_points_by_id[50827] = {'display_title': 'Humidity', 'is_dynamic': True}
        em.dynamic_point_map._table[3920] = DynamicPointEntry(
            point_id=3920, title='Additional heating enable', entity_type='switch',
            dynamic_points_by_value={1: [50827]},
        )
        _, message, _ = em.build_disable_notification(
            50827, 'sensor.humidity', 'disabled',
        )
        expected = (
            'Dynamic data point #50827 (Humidity) was disabled via the HA entity settings. '
            'The bridge has kept the entity enabled — it is still being polled.\n\n'
            'Please go to Settings > Entities and re-enable it.\n\n'
            'This entity appeared because of a change to '
            '"Additional heating enable" (#3920). '
            'It will disappear automatically when that switch/select '
            'is no longer in the state that activates it.'
        )
        self.assertEqual(message, expected)

    def test_dynamic_point_without_controller_exact_message(self):
        """Pins the exact wording of the dynamic-with-no-known-controller
        (generic firmware-controlled) message body."""
        em = _make_em()
        em.all_points_by_id[50827] = {'display_title': 'Humidity', 'is_dynamic': True}
        _, message, _ = em.build_disable_notification(
            50827, 'sensor.humidity', 'disabled',
        )
        expected = (
            'Dynamic data point #50827 (Humidity) was disabled via the HA entity settings. '
            'The bridge has kept the entity enabled — it is still being polled.\n\n'
            'Please go to Settings > Entities and re-enable it.\n\n'
            'This entity appeared during a firmware-controlled state change. '
            'It will disappear automatically when the operating mode that '
            'activates it is no longer active.'
        )
        self.assertEqual(message, expected)


class TestDeindexPoint(unittest.TestCase):
    """_deindex_point removes a point from all_points_by_id and
    _point_string_cache — never directly tested, always mocked in callers."""

    def test_removes_correct_point_id_from_all_points_by_id(self):
        """A wrong key (e.g. None) would leave the real point_id still
        indexed forever, while popping an unrelated no-op entry."""
        em = _make_em()
        em.all_points_by_id[100] = {'variableId': 100}
        em.all_points_by_id[200] = {'variableId': 200}
        em._deindex_point(100)
        self.assertNotIn(100, em.all_points_by_id)
        self.assertIn(200, em.all_points_by_id)

    def test_absent_point_id_does_not_raise(self):
        """Deindexing a point_id not currently indexed must be a no-op,
        not a KeyError — a missing .pop() default would crash here."""
        em = _make_em()
        em._deindex_point(999999)  # must not raise


class TestDecrementStats(unittest.TestCase):
    """_decrement_stats mirrors _increment_stats, clamped at 0 — covers
    the actual decrement amount and the writable-count clamp floor, which
    had zero prior test coverage."""

    def test_type_count_decrements_by_exactly_one(self):
        """A wrong decrement amount (e.g. -2) would only be visible when
        the starting count is above 1, since both -1 and -2 clamp to the
        same 0 from a starting count of 1."""
        em = _make_em()
        em._stats_type_counts['sensor'] = 3
        em._decrement_stats({'entity_type': 'sensor', 'entity_category': 'diagnostic'})
        self.assertEqual(em._stats_type_counts['sensor'], 2)

    def test_writable_count_default_false_when_key_absent(self):
        """A point dict missing 'is_writable' entirely must not decrement
        the writable count — a wrong default (True) would silently
        under-count writable entities on every point lacking the key."""
        em = _make_em()
        em._stats_writable_count = 5
        em._decrement_stats({'entity_type': 'sensor', 'entity_category': 'diagnostic'})
        self.assertEqual(em._stats_writable_count, 5)

    def test_writable_count_clamped_at_zero_not_one(self):
        """Decrementing from 1 must clamp to 0, not 1 — a mutated floor of
        max(1, ...) would leave a phantom writable entity counted forever."""
        em = _make_em()
        em._stats_writable_count = 1
        em._decrement_stats({'entity_type': 'sensor', 'entity_category': 'diagnostic',
                             'is_writable': True})
        self.assertEqual(em._stats_writable_count, 0)

    @given(st.integers(min_value=0, max_value=1000))
    def test_writable_count_decrement_is_exactly_max_0_count_minus_1(self, count):
        """For any non-negative starting count, one decrement must equal
        exactly max(0, count - 1) — generalizes the two hand-picked
        boundary examples above (3->2, 1->0) to the whole space, including
        count=0 itself (already-clamped, must stay 0)."""
        em = _make_em()
        em._stats_writable_count = count
        em._decrement_stats({'entity_type': 'sensor', 'entity_category': 'diagnostic',
                             'is_writable': True})
        self.assertEqual(em._stats_writable_count, max(0, count - 1))

    @given(st.integers(min_value=0, max_value=1000))
    def test_type_count_decrement_is_exactly_max_0_count_minus_1(self, count):
        em = _make_em()
        em._stats_type_counts['sensor'] = count
        em._decrement_stats({'entity_type': 'sensor', 'entity_category': 'diagnostic'})
        self.assertEqual(em._stats_type_counts['sensor'], max(0, count - 1))

    def test_missing_entity_type_key_decrements_the_unknown_bucket(self):
        """_increment_stats and _decrement_stats must agree on the exact
        same fallback key ('unknown') for a point dict missing
        'entity_type' entirely, since _increment_stats is what created
        that dict entry in the first place. A default-value or key-name
        drift between the two functions (e.g. 'unknown' vs 'UNKNOWN', or
        None) makes the `if entity_type_key in self._stats_type_counts`
        lookup miss, silently leaving that counter stuck too high forever
        — every prior test always supplied 'entity_type' explicitly, so
        this fallback path had zero coverage."""
        em = _make_em()
        em._increment_stats({'entity_category': 'diagnostic'})
        self.assertEqual(em._stats_type_counts.get('unknown'), 1)
        em._decrement_stats({'entity_category': 'diagnostic'})
        self.assertEqual(em._stats_type_counts.get('unknown'), 0)

    def test_missing_entity_category_key_decrements_the_none_bucket(self):
        """Same symmetry requirement as the entity_type case above, but
        for 'entity_category' and its 'none' fallback key."""
        em = _make_em()
        em._increment_stats({'entity_type': 'sensor'})
        self.assertEqual(em._stats_category_counts.get('none'), 1)
        em._decrement_stats({'entity_type': 'sensor'})
        self.assertEqual(em._stats_category_counts.get('none'), 0)


class TestAppliedModePersistence(unittest.TestCase):
    """Covers read_applied_mode(), _persist_applied_mode(),
    _read_applied_mode_from_file(), and record_applied_mode() — the
    mechanism decide_startup_action relies on to detect a genuine mode
    change across a restart. read_applied_mode() uses the same
    synchronous subscribe-and-wait pattern as scan_mqtt_discovery(); tests
    simulate immediate retained-message delivery by having the mocked
    message_callback_add invoke the real callback synchronously, so the
    real method body runs with zero wall-clock wait."""

    def setUp(self):
        import os
        import tempfile
        self._tmp_dir = tempfile.mkdtemp()
        self._tmp_path = os.path.join(self._tmp_dir, 'applied_mode')

    def _deliver_retained(self, em, payload: bytes | None):
        """Make em.mqtt.message_callback_add synchronously invoke the
        stored callback with a fake retained message — simulating the
        broker responding before the .wait() timeout would otherwise fire."""
        def fake_callback_add(_topic, cb):
            if payload is None:
                return  # simulate no retained message — real timeout path
            msg = MagicMock()
            msg.payload = payload
            cb(None, None, msg)
        em.mqtt.message_callback_add = MagicMock(side_effect=fake_callback_add)

    def test_read_applied_mode_returns_mqtt_value(self):
        em = _make_em()
        self._deliver_retained(em, b'menus')
        self.assertEqual(em.read_applied_mode(), 'menus')

    def test_read_applied_mode_invalid_utf8_payload_falls_back_to_file(self):
        """A malformed retained payload must not raise — decode failure is
        caught and treated as no MQTT value, falling through to the file."""
        em = _make_em()
        self._deliver_retained(em, b'\xff\xfe\x00\x01')  # invalid UTF-8
        with open(self._tmp_path, 'w') as f:
            f.write('advanced')
        with patch('nibe_entity_manager._APPLIED_MODE_FILE', self._tmp_path):
            self.assertEqual(em.read_applied_mode(), 'advanced')

    def test_read_applied_mode_strips_whitespace(self):
        em = _make_em()
        self._deliver_retained(em, b'  advanced  \n')
        self.assertEqual(em.read_applied_mode(), 'advanced')

    def test_read_applied_mode_empty_payload_falls_back_to_file(self):
        """An empty retained payload (topic exists but was cleared) must be
        treated the same as no message — fall through to the file."""
        em = _make_em()
        self._deliver_retained(em, b'')
        with open(self._tmp_path, 'w') as f:
            f.write('monitoring')
        with patch('nibe_entity_manager._APPLIED_MODE_FILE', self._tmp_path):
            self.assertEqual(em.read_applied_mode(), 'monitoring')

    def test_read_applied_mode_falls_back_to_file_when_no_mqtt_message(self):
        """The real migration-boundary / timeout path: no retained message
        arrives at all — timeout fires and file fallback is used."""
        em = _make_em()
        self._deliver_retained(em, None)
        with open(self._tmp_path, 'w') as f:
            f.write('all')
        with patch('nibe_entity_manager._APPLIED_MODE_FILE', self._tmp_path), \
             patch('nibe_entity_manager._APPLIED_MODE_TIMEOUT_S', 0):
            self.assertEqual(em.read_applied_mode(), 'all')

    def test_read_applied_mode_returns_none_when_neither_store_has_a_record(self):
        em = _make_em()
        self._deliver_retained(em, None)
        with patch('nibe_entity_manager._APPLIED_MODE_FILE', self._tmp_path), \
             patch('nibe_entity_manager._APPLIED_MODE_TIMEOUT_S', 0):
            self.assertIsNone(em.read_applied_mode())  # tmp file doesn't exist

    def test_read_applied_mode_unsubscribes_after_wait(self):
        """Must always clean up its temporary subscription, whether or not
        a message arrived."""
        em = _make_em()
        self._deliver_retained(em, b'menus')
        em.read_applied_mode()
        em.mqtt.unsubscribe.assert_called_once()
        em.mqtt.message_callback_remove.assert_called_once()

    def test_read_applied_mode_uses_correct_topic_throughout(self):
        """All four MQTT calls (subscribe, callback_add, callback_remove,
        unsubscribe) must use the real APPLIED_MODE topic — a wrong/None
        topic on any of them would make this listen on (or fail to clean
        up) the wrong topic, since a MagicMock accepts any argument
        silently and every other test here only checks call COUNTS."""
        from nibe_mqtt_publisher import BrowserTopic
        em = _make_em()
        self._deliver_retained(em, b'menus')
        em.read_applied_mode()
        em.mqtt.subscribe.assert_any_call(BrowserTopic.APPLIED_MODE)
        em.mqtt.message_callback_add.assert_any_call(
            BrowserTopic.APPLIED_MODE, unittest.mock.ANY)
        em.mqtt.message_callback_remove.assert_any_call(BrowserTopic.APPLIED_MODE)
        em.mqtt.unsubscribe.assert_any_call(BrowserTopic.APPLIED_MODE)

    def test_persist_applied_mode_writes_file_then_mqtt(self):
        """Write-ahead: file first, then the retained MQTT topic."""
        from nibe_mqtt_publisher import BrowserTopic
        em = _make_em()
        em._persist_applied_mode('essential', path=self._tmp_path)
        with open(self._tmp_path) as f:
            self.assertEqual(f.read(), 'essential')
        em.mqtt.publish.assert_called_once_with(
            BrowserTopic.APPLIED_MODE, 'essential', retain=True
        )

    def test_persist_applied_mode_tolerates_unwritable_file(self):
        """A failed file write (e.g. /data/ not present) must not prevent
        the MQTT publish — the file is a fallback, not the primary store."""
        from nibe_mqtt_publisher import BrowserTopic
        em = _make_em()
        bad_path = '/nonexistent-dir/applied_mode'
        em._persist_applied_mode('advanced', path=bad_path)  # must not raise
        em.mqtt.publish.assert_called_once_with(
            BrowserTopic.APPLIED_MODE, 'advanced', retain=True
        )

    def test_read_applied_mode_from_file_defaults_to_production_path(self):
        """_read_applied_mode_from_file() with no explicit path must read
        from _APPLIED_MODE_FILE, not silently no-op."""
        from nibe_entity_manager import _APPLIED_MODE_FILE
        em = _make_em()
        with patch('builtins.open', mock_open(read_data='menus')) as m:
            result = em._read_applied_mode_from_file()
        m.assert_called_once_with(_APPLIED_MODE_FILE, encoding='utf-8')
        self.assertEqual(result, 'menus')

    def test_read_applied_mode_from_file_returns_none_when_absent(self):
        em = _make_em()
        self.assertIsNone(em._read_applied_mode_from_file('/nonexistent-dir/applied_mode'))

    def test_read_applied_mode_from_file_strips_whitespace(self):
        em = _make_em()
        with open(self._tmp_path, 'w') as f:
            f.write('  menus\n')
        self.assertEqual(em._read_applied_mode_from_file(self._tmp_path), 'menus')

    def test_read_applied_mode_from_file_empty_content_returns_none(self):
        em = _make_em()
        with open(self._tmp_path, 'w') as f:
            f.write('   ')
        self.assertIsNone(em._read_applied_mode_from_file(self._tmp_path))

    def test_persist_then_read_round_trip_through_a_real_file(self):
        """_persist_applied_mode() and _read_applied_mode_from_file() are
        each unit-tested above, but every existing test verifies the write
        side via a raw open()/read() and the read side via a raw
        open()/write() — never chains the two real functions through the
        same real file, which is exactly the seam a restart depends on:
        this process's _persist_applied_mode() call writing something the
        NEXT process's _read_applied_mode_from_file() call can actually
        read back correctly. Uses two separate EntityManager instances to
        simulate that restart, not just two calls on the same one."""
        writer_em = _make_em()
        writer_em._persist_applied_mode('advanced', path=self._tmp_path)

        reader_em = _make_em()
        self.assertEqual(
            reader_em._read_applied_mode_from_file(self._tmp_path), 'advanced',
        )

    def test_persist_then_read_round_trip_survives_repeated_writes(self):
        """A second real persist to the same file must fully replace the
        first — not append or leave stale trailing content that could
        corrupt the strip()ped read (e.g. 'essentialadvanced' or
        'advanced\\nessential' if the file weren't truncated on rewrite)."""
        em = _make_em()
        em._persist_applied_mode('essential', path=self._tmp_path)
        em._persist_applied_mode('advanced', path=self._tmp_path)
        self.assertEqual(
            em._read_applied_mode_from_file(self._tmp_path), 'advanced',
        )

    def test_record_applied_mode_persists_without_touching_enabled_set(self):
        """record_applied_mode is the migration-boundary helper — it must
        record the baseline without enabling or disabling anything."""
        em = _make_em()
        em.mqtt_enabled_points = {1, 2, 3}
        with patch.object(em, '_persist_applied_mode') as mock_persist:
            em.record_applied_mode('essential')
        mock_persist.assert_called_once_with('essential')
        self.assertEqual(em.mqtt_enabled_points, {1, 2, 3})  # unchanged


class TestApplyMode(unittest.TestCase):
    """apply_mode() replaced the old strictly-additive apply_preset(). It now
    both enables points newly required by the target mode AND disables
    points that are enabled but not part of it — except active dynamic
    points, which must never be touched by a mode change since their
    existence is firmware-state-driven, not mode-driven. The dynamic-point
    protection test below is the highest-risk case in this refactor: get it
    wrong and a mode change silently kills a live dynamic entity."""

    def _all_points(self, ids):
        return {pid: {'title': f'Point {pid}'} for pid in ids}

    def setUp(self):
        # Applied-mode persistence writes to /data/applied_mode as a file
        # fallback; redirect to a throwaway path so tests don't touch the
        # real filesystem (a missing /data/ is caught safely anyway, but
        # this keeps test output clean and hermetic).
        import os
        import tempfile
        self._tmp_mode_file = os.path.join(tempfile.mkdtemp(), 'applied_mode')
        patcher = patch('nibe_entity_manager._APPLIED_MODE_FILE', self._tmp_mode_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_known_mode_enables_its_points(self):
        em = _make_em()
        em.all_points_by_id = self._all_points([1, 2, 3])
        with patch('nibe_entity_manager.MODES', {'essential': frozenset({1, 2})}):
            em.apply_mode('essential')
        self.assertEqual(em.mqtt_enabled_points, {1, 2})

    def test_nested_call_does_not_prematurely_clear_outer_suppression(self):
        """was_suppressed must reflect the REAL depth (> 0), not always be
        falsy — if apply_mode runs while an outer caller already holds
        suppression (depth > 0), it must not increment OR decrement the
        depth counter itself, since the outer caller owns that lifecycle.
        A mutation making was_suppressed always falsy would make a nested
        call prematurely decrement the outer suppression's depth,
        potentially letting publish_enabled_state() fire mid-outer-operation
        or driving the counter negative."""
        em = _make_em()
        em.all_points_by_id = self._all_points([1, 2])
        # Simulate an outer caller already holding suppression (depth=1).
        em._suppress_enabled_state_depth = 1
        with patch('nibe_entity_manager.MODES', {'essential': frozenset({1, 2})}):
            em.apply_mode('essential')
        # apply_mode must leave the depth exactly as the outer caller left
        # it — neither incremented (it should skip that, since already
        # suppressed) nor decremented below what the outer caller expects.
        self.assertEqual(em._suppress_enabled_state_depth, 1)

    def test_nested_call_does_not_re_increment_depth_during_loop(self):
        """Complements test_nested_call_does_not_prematurely_clear_outer_
        suppression: that test only checks the depth AFTER apply_mode
        returns, which a mutated was_suppressed computation can still get
        right by re-incrementing then re-decrementing in a self-cancelling
        way (e.g. was_suppressed always False still nets back to the
        starting depth). This test instead observes the depth DURING the
        enable/disable loop itself, which exposes exactly that class of
        mutation: if was_suppressed is computed wrong (e.g. always falsy,
        or using '> 1' instead of '> 0'), apply_mode incorrectly
        re-increments the already-positive depth to one higher than the
        outer caller set it to, even though the final value looks fine
        afterward."""
        em = _make_em()
        em.all_points_by_id = self._all_points([1, 2])
        em.mqtt_enabled_points = {2}
        em._suppress_enabled_state_depth = 1  # outer caller already suppressing
        observed_depths = []
        original_enable  = em._enable_entity_locked
        original_disable = em._disable_entity_locked

        def spy_enable(point_id):
            observed_depths.append(em._suppress_enabled_state_depth)
            return original_enable(point_id)

        def spy_disable(point_id):
            observed_depths.append(em._suppress_enabled_state_depth)
            return original_disable(point_id)

        with patch('nibe_entity_manager.MODES', {'essential': frozenset({1})}), \
             patch.object(em, '_enable_entity_locked', side_effect=spy_enable), \
             patch.object(em, '_disable_entity_locked', side_effect=spy_disable):
            em.apply_mode('essential')

        # The depth must stay exactly at the outer caller's value (1)
        # throughout the loop — apply_mode must not touch it at all since
        # it didn't own the suppression in the first place.
        self.assertTrue(observed_depths, "loop bodies were never invoked")
        for depth in observed_depths:
            self.assertEqual(depth, 1)

    def test_mode_change_disables_points_not_in_new_mode(self):
        """The core behavioral change vs the old additive apply_preset:
        switching mode must prune points that belonged to the old set."""
        em = _make_em()
        em.all_points_by_id = self._all_points([1, 2, 3, 4])
        em.mqtt_enabled_points = {3, 4}  # enabled under a previous mode
        with patch('nibe_entity_manager.MODES', {'essential': frozenset({1, 2})}):
            em.apply_mode('essential')
        self.assertEqual(em.mqtt_enabled_points, {1, 2})

    def test_mode_switch_behavior_replace_is_the_default(self):
        """mode_switch_behavior defaults to 'replace' on a freshly
        constructed EntityManager — generate_nibe_mqtt.py only overrides
        this post-construction if cfg.mode_switch_behavior differs, so the
        default itself must already be the safe, existing prune behaviour."""
        em = _make_em()
        self.assertEqual(em.mode_switch_behavior, 'replace')

    def test_mode_switch_behavior_merge_does_not_disable_anything(self):
        """With mode_switch_behavior='merge', points enabled under a
        previous mode must survive a mode change even though they are not
        part of the new mode's set — only 'replace' (the default) prunes."""
        em = _make_em()
        em.all_points_by_id = self._all_points([1, 2, 3, 4])
        em.mqtt_enabled_points = {3, 4}
        em.mode_switch_behavior = 'merge'
        with patch('nibe_entity_manager.MODES', {'essential': frozenset({1, 2})}):
            em.apply_mode('essential')
        self.assertEqual(em.mqtt_enabled_points, {1, 2, 3, 4})

    def test_mode_switch_behavior_merge_still_enables_new_points(self):
        """'merge' must still enable the new mode's points — it only skips
        the disable side, it isn't a no-op."""
        em = _make_em()
        em.all_points_by_id = self._all_points([1, 2, 3])
        em.mqtt_enabled_points = {3}
        em.mode_switch_behavior = 'merge'
        with patch('nibe_entity_manager.MODES', {'essential': frozenset({1, 2})}):
            em.apply_mode('essential')
        self.assertEqual(em.mqtt_enabled_points, {1, 2, 3})

    def test_active_dynamic_points_protected_from_disable(self):
        """A mode change must never disable a currently-active dynamic
        point even though it isn't a member of the target mode's static
        point set — its existence is firmware-state-driven, not
        mode-driven. This is the highest-risk case in the reconcile path."""
        em = _make_em()
        em.all_points_by_id = self._all_points([1, 2, 99])
        em.mqtt_enabled_points = {2, 99}
        em.active_dynamic_points = {99}  # live dynamic entity, not in target
        with patch('nibe_entity_manager.MODES', {'essential': frozenset({1})}):
            em.apply_mode('essential')
        self.assertIn(99, em.mqtt_enabled_points, "dynamic point must survive the mode change")
        self.assertNotIn(2, em.mqtt_enabled_points)
        self.assertIn(1, em.mqtt_enabled_points)

    def test_already_enabled_point_in_mode_not_re_enabled(self):
        em = _make_em()
        em.all_points_by_id = self._all_points([1, 2])
        em.mqtt_enabled_points = {1}
        with patch('nibe_entity_manager.MODES', {'essential': frozenset({1, 2})}), \
             patch.object(em, '_enable_entity_locked', wraps=em._enable_entity_locked) as spy:
            em.apply_mode('essential')
        spy.assert_called_once_with(2)

    def test_all_mode_enables_every_known_point(self):
        """The 'all' mode is a sentinel (MODES['all'] is None in the real
        table) handled as a special case, not a literal None lookup result
        being silently treated as 'enable nothing'."""
        em = _make_em()
        em.all_points_by_id = self._all_points([1, 2, 3])
        with patch('nibe_entity_manager.MODES', {'all': None}):
            em.apply_mode('all')
        self.assertEqual(em.mqtt_enabled_points, {1, 2, 3})

    def test_unrecognized_mode_name_disables_everything_except_dynamic(self):
        """Unlike the old additive apply_preset (an unknown name enabled
        nothing and touched nothing else), apply_mode's reconcile means an
        unrecognized name resolves to an empty target set and disables
        every non-dynamic enabled point. The add-on's config schema
        prevents this via a fixed choice list, but the method itself must
        behave predictably rather than crash on a bad name."""
        em = _make_em()
        em.all_points_by_id = self._all_points([1, 2, 3])
        em.mqtt_enabled_points = {1, 2}
        em.active_dynamic_points = {2}
        with patch('nibe_entity_manager.MODES', {}):
            em.apply_mode('totally_unknown_mode')
        self.assertEqual(em.mqtt_enabled_points, {2})  # only the protected dynamic point survives

    def test_empty_frozenset_mode_enables_nothing_new(self):
        """The real 'none' mode is an empty frozenset — distinct from a
        missing key, must also result in zero new enables without error."""
        em = _make_em()
        em.all_points_by_id = self._all_points([1, 2, 3])
        with patch('nibe_entity_manager.MODES', {'none': frozenset()}):
            em.apply_mode('none')
        self.assertEqual(em.mqtt_enabled_points, set())

    def test_publish_enabled_state_called_once_at_end(self):
        """publish_enabled_state must fire exactly once after the whole
        batch, not once per point — confirms _suppress_enabled_state is
        actually wrapping the enable+disable loop."""
        em = _make_em()
        em.all_points_by_id = self._all_points([1, 2, 3])
        with patch('nibe_entity_manager.MODES', {'essential': frozenset({1, 2, 3})}), \
             patch.object(em, 'publish_enabled_state') as mock_publish:
            em.apply_mode('essential')
        mock_publish.assert_called_once()

    def test_suppression_active_during_enable_and_disable_loop(self):
        """Confirms _is_suppressed() is genuinely True while points are
        being enabled/disabled — the suppress context manager actually
        wraps the loop, not just decorates it cosmetically.
        apply_mode calls _enable_entity_locked/_disable_entity_locked
        directly (already holding _em_lock), so the spy wraps those
        internal methods rather than the public wrappers."""
        em = _make_em()
        em.all_points_by_id = self._all_points([1, 2])
        em.mqtt_enabled_points = {2}
        observed = {}
        original_enable  = em._enable_entity_locked
        original_disable = em._disable_entity_locked

        def spy_enable(point_id):
            observed['suppressed_during_enable'] = em._is_suppressed()
            return original_enable(point_id)

        def spy_disable(point_id):
            observed['suppressed_during_disable'] = em._is_suppressed()
            return original_disable(point_id)

        with patch('nibe_entity_manager.MODES', {'essential': frozenset({1})}), \
             patch.object(em, '_enable_entity_locked', side_effect=spy_enable), \
             patch.object(em, '_disable_entity_locked', side_effect=spy_disable):
            em.apply_mode('essential')

        self.assertTrue(observed['suppressed_during_enable'])
        self.assertTrue(observed['suppressed_during_disable'])
        self.assertFalse(em._is_suppressed())  # released after the call

    def test_point_not_in_all_points_by_id_skipped_gracefully(self):
        """A mode referencing a point_id not present in all_points_by_id
        (e.g. a mode table entry for a point this firmware doesn't have)
        must not crash and must not be enabled — the target set is
        intersected with all_points_by_id before diffing."""
        em = _make_em()
        em.all_points_by_id = self._all_points([1])  # 2 deliberately absent
        with patch('nibe_entity_manager.MODES', {'essential': frozenset({1, 2})}):
            em.apply_mode('essential')  # must not raise
        self.assertIn(1, em.mqtt_enabled_points)
        self.assertNotIn(2, em.mqtt_enabled_points)

    def test_persists_applied_mode_to_mqtt(self):
        """apply_mode must record the mode it just reconciled to, via the
        retained BrowserTopic.APPLIED_MODE topic — this is what
        decide_startup_action reads on the next restart."""
        from nibe_mqtt_publisher import BrowserTopic
        em = _make_em()
        em.all_points_by_id = self._all_points([1])
        with patch('nibe_entity_manager.MODES', {'essential': frozenset({1})}):
            em.apply_mode('essential')
        published = {c.args[0]: c.args[1] for c in em.mqtt.publish.call_args_list}
        self.assertEqual(published.get(BrowserTopic.APPLIED_MODE), 'essential')

    def test_enabled_points_are_marked_wanted(self):
        """Points apply_mode enables must join _wanted_points so they're
        automatically re-enabled if they later disappear from bulk data
        outside the dynamic-tracking mechanism and reappear."""
        em = _make_em()
        em.all_points_by_id = self._all_points([1, 2])
        with patch('nibe_entity_manager.MODES', {'essential': frozenset({1, 2})}):
            em.apply_mode('essential')
        self.assertEqual(em._wanted_points, {1, 2})

    def test_mode_driven_disable_removes_from_wanted(self):
        """A point dropped by a mode switch is an intentional override, not
        a firmware-absence artifact — it must be removed from
        _wanted_points so _reconcile_wanted_points doesn't fight the
        mode change by re-enabling it the next time it's seen in bulk data."""
        em = _make_em()
        em.all_points_by_id = self._all_points([1, 2, 3])
        em.mqtt_enabled_points = {3}
        em._wanted_points = {3}
        with patch('nibe_entity_manager.MODES', {'essential': frozenset({1, 2})}):
            em.apply_mode('essential')
        self.assertNotIn(3, em._wanted_points)
        self.assertEqual(em._wanted_points, {1, 2})


class TestWantedPointsPersistence(unittest.TestCase):
    """Covers _persist_wanted_points()/_mark_wanted()/_unmark_wanted() and
    the MQTT-restore/file-fallback loading paths for _wanted_points — the
    catch-all safety net that re-enables a user-enabled point if it ever
    disappears from bulk data (e.g. via the generic "absent from bulk
    data" fallback, not the dynamic-tracking mechanism) and later reappears."""

    def setUp(self):
        import os
        import tempfile
        self._tmp_dir = tempfile.mkdtemp()
        self._tmp_path = os.path.join(self._tmp_dir, 'wanted_points.json')

    def test_persist_writes_file_then_mqtt(self):
        """Write-ahead: file first, then the retained MQTT topic."""
        from nibe_mqtt_publisher import BrowserTopic
        em = _make_em()
        em._wanted_points = {3, 1, 2}
        em._persist_wanted_points(path=self._tmp_path)
        with open(self._tmp_path) as f:
            self.assertEqual(json.loads(f.read()), [1, 2, 3])
        em.mqtt.publish.assert_called_once_with(
            BrowserTopic.WANTED_POINTS, '[1, 2, 3]', retain=True
        )

    def test_persist_tolerates_unwritable_file(self):
        """A failed file write (e.g. /data/ not present) must not prevent
        the MQTT publish — the broker is the primary store."""
        from nibe_mqtt_publisher import BrowserTopic
        em = _make_em()
        em._wanted_points = {5}
        em._persist_wanted_points(path='/nonexistent-dir/wanted_points.json')
        em.mqtt.publish.assert_called_once_with(
            BrowserTopic.WANTED_POINTS, '[5]', retain=True
        )

    def test_mark_wanted_adds_and_persists_once(self):
        em = _make_em()
        em._mark_wanted(7)
        self.assertEqual(em._wanted_points, {7})
        self.assertEqual(em.mqtt.publish.call_count, 1)
        # Marking an already-wanted point again must be a no-op — no
        # redundant persist.
        em._mark_wanted(7)
        self.assertEqual(em.mqtt.publish.call_count, 1)

    def test_unmark_wanted_removes_and_persists_once(self):
        em = _make_em()
        em._wanted_points = {7}
        em._unmark_wanted(7)
        self.assertEqual(em._wanted_points, set())
        self.assertEqual(em.mqtt.publish.call_count, 1)
        # Unmarking a point that isn't wanted must be a no-op.
        em._unmark_wanted(7)
        self.assertEqual(em.mqtt.publish.call_count, 1)

    def test_enable_entity_marks_wanted(self):
        em = _make_em()
        em.all_points_by_id = {1: {'variableId': 1, 'title': 'Point 1'}}
        with patch.object(em, '_enable_entity_locked', return_value=True):
            em.enable_entity(1)
        self.assertIn(1, em._wanted_points)

    def test_enable_entity_failure_does_not_mark_wanted(self):
        em = _make_em()
        with patch.object(em, '_enable_entity_locked', return_value=False):
            em.enable_entity(99)
        self.assertNotIn(99, em._wanted_points)

    def test_disable_entity_default_removes_wanted(self):
        em = _make_em()
        em._wanted_points = {1}
        with patch.object(em, '_disable_entity_locked', return_value=True):
            em.disable_entity(1)
        self.assertNotIn(1, em._wanted_points)

    def test_disable_entity_remove_from_wanted_false_keeps_wanted(self):
        em = _make_em()
        em._wanted_points = {1}
        with patch.object(em, '_disable_entity_locked', return_value=True):
            em.disable_entity(1, remove_from_wanted=False)
        self.assertIn(1, em._wanted_points)

    def test_reconcile_wanted_points_reenables_reappeared_point(self):
        em = _make_em()
        em._wanted_points = {1, 2}
        em.mqtt_enabled_points = set()
        with patch.object(em, '_enable_entity_locked', return_value=True) as mock_enable:
            em._reconcile_wanted_points({1})
        mock_enable.assert_called_once_with(1)

    def test_reconcile_wanted_points_skips_already_enabled(self):
        em = _make_em()
        em._wanted_points = {1}
        em.mqtt_enabled_points = {1}
        with patch.object(em, '_enable_entity_locked') as mock_enable:
            em._reconcile_wanted_points({1})
        mock_enable.assert_not_called()

    def test_reconcile_wanted_points_skips_still_absent(self):
        em = _make_em()
        em._wanted_points = {1}
        em.mqtt_enabled_points = set()
        with patch.object(em, '_enable_entity_locked') as mock_enable:
            em._reconcile_wanted_points(set())
        mock_enable.assert_not_called()


class TestApplyModeNone(unittest.TestCase):
    """Test apply_mode with 'none' mode."""

    def test_none_mode_leaves_dynamic_points_enabled(self):
        em = _make_em()
        em.all_points_by_id = {1: {'title': 'Static1'}, 2: {'title': 'Dynamic'}}
        em.mqtt_enabled_points = {1, 2}
        em.active_dynamic_points = {2}
        with patch('nibe_entity_manager.MODES', {'none': frozenset()}):
            em.apply_mode('none')
        self.assertEqual(em.mqtt_enabled_points, {2})
        self.assertNotIn(1, em.mqtt_enabled_points)


class TestEnableEntityMissingPointLogLevel(unittest.TestCase):
    """enable_entity logs WARNING (not ERROR) when a point is absent from
    bulk data. ERROR implied something broken; WARNING is correct since
    conditional points like 3671/5033 are legitimately absent when a room
    sensor is installed."""

    def test_missing_point_logs_warning_not_error(self):
        em = _make_em()
        with self.assertLogs('nibe.entities', level='WARNING') as cm:
            result = em.enable_entity(99999)
        self.assertFalse(result)
        # Must be WARNING, not ERROR
        self.assertTrue(any('WARNING' in line for line in cm.output),
            "Missing point must log at WARNING level")
        self.assertFalse(any('ERROR' in line for line in cm.output),
            "Missing point must NOT log at ERROR level")

    def test_missing_point_message_mentions_conditional(self):
        em = _make_em()
        with self.assertLogs('nibe.entities', level='WARNING') as cm:
            em.enable_entity(99999)
        self.assertTrue(any('bulk data' in line or 'conditional' in line
                             for line in cm.output))


class TestRepublishAvailability(unittest.TestCase):
    """republish_availability publishes 'online' for all active entities
    after a broker restart."""

    def test_publishes_online_for_all_active_entities(self):
        em = _make_em()
        em.active_entities_by_id[100] = {
            'availability_topic': 'homeassistant/sensor/nibe_100/available'
        }
        em.active_entities_by_id[200] = {
            'availability_topic': 'homeassistant/sensor/nibe_200/available'
        }
        em.republish_availability()
        topics = [c[0][0] for c in em.mqtt.publish.call_args_list]
        self.assertIn('homeassistant/sensor/nibe_100/available', topics)
        self.assertIn('homeassistant/sensor/nibe_200/available', topics)

    def test_all_published_as_online(self):
        em = _make_em()
        em.active_entities_by_id[100] = {
            'availability_topic': 'homeassistant/sensor/nibe_100/available'
        }
        em.republish_availability()
        avail_calls = [c for c in em.mqtt.publish.call_args_list
                       if 'available' in c[0][0]]
        self.assertTrue(all(c[0][1] == 'online' for c in avail_calls))

    def test_no_publish_when_no_active_entities(self):
        em = _make_em()
        em.active_entities_by_id.clear()
        em.republish_availability()
        em.mqtt.publish.assert_not_called()

    def test_mgmt_avail_topic_published_when_set(self):
        em = _make_em()
        em.active_entities_by_id[100] = {
            'availability_topic': 'homeassistant/sensor/nibe_100/available'
        }
        em._mgmt_avail_topic = 'homeassistant/nibe/management/available'
        em.republish_availability()
        topics = [c[0][0] for c in em.mqtt.publish.call_args_list]
        self.assertIn('homeassistant/nibe/management/available', topics)

    def test_entity_availability_published_with_retain_true(self):
        """Availability must be retained — otherwise HA (which may itself
        reconnect after this republish) sees no availability state until
        the next unrelated poll cycle, wrongly showing entities as
        unavailable in the meantime."""
        em = _make_em()
        em.active_entities_by_id[100] = {
            'availability_topic': 'homeassistant/sensor/nibe_100/available'
        }
        em.republish_availability()
        avail_calls = [c for c in em.mqtt.publish.call_args_list
                       if c.args[0] == 'homeassistant/sensor/nibe_100/available']
        self.assertTrue(avail_calls)
        for c in avail_calls:
            retain = c.kwargs.get('retain', c.args[2] if len(c.args) > 2 else None)
            self.assertTrue(retain)

    def test_mgmt_avail_topic_published_as_online_with_retain_true(self):
        em = _make_em()
        em.active_entities_by_id[100] = {
            'availability_topic': 'homeassistant/sensor/nibe_100/available'
        }
        em._mgmt_avail_topic = 'homeassistant/nibe/management/available'
        em.republish_availability()
        mgmt_calls = [c for c in em.mqtt.publish.call_args_list
                      if c.args[0] == 'homeassistant/nibe/management/available']
        self.assertTrue(mgmt_calls)
        for c in mgmt_calls:
            self.assertEqual(c.args[1], 'online')
            retain = c.kwargs.get('retain', c.args[2] if len(c.args) > 2 else None)
            self.assertTrue(retain)


class TestPublishEnabledStateCallbackException(unittest.TestCase):
    """publish_enabled_state catches exceptions from the change callback."""

    def test_callback_exception_does_not_raise(self):
        em = _make_em()
        em._on_enabled_state_change = MagicMock(side_effect=RuntimeError("boom"))
        em.mqtt_enabled_points.add(1)
        em.publish_enabled_state()   # must not raise
        em._on_enabled_state_change.assert_called_once()


class TestPublishEnabledStateCallback(unittest.TestCase):
    """Test the enabled state change callback behaviour."""

    def test_callback_not_called_when_set_unchanged(self):
        em = _make_em()
        callback = MagicMock()
        em.set_on_enabled_state_change(callback)
        em.mqtt_enabled_points = {1, 2}
        em._last_published_enabled = frozenset({1, 2})
        em.publish_enabled_state()
        callback.assert_not_called()

    def test_callback_called_when_set_changes(self):
        em = _make_em()
        callback = MagicMock()
        em.set_on_enabled_state_change(callback)
        em.mqtt_enabled_points = {1, 2, 3}
        em._last_published_enabled = frozenset({1, 2})
        em.publish_enabled_state()
        callback.assert_called_once()

    def test_publishes_real_enabled_points_to_frontend(self):
        """The frontend card's enabled-point list must be the real
        mqtt_enabled_points set, not None — this is the actual data
        the card displays, not just an internal bookkeeping detail."""
        em = _make_em()
        em.mqtt_enabled_points = {1, 2, 3}
        em.publish_enabled_state()
        em._pub.publish_enabled_state.assert_called_once_with({1, 2, 3})

    def test_last_published_enabled_updated_prevents_repeat_callback(self):
        """_last_published_enabled must be updated to the REAL current
        set, not None — otherwise a second call with unchanged state
        would incorrectly fire the callback again (None never equals a
        real frozenset), defeating the documented purpose of avoiding
        redundant dashboard regeneration triggers."""
        em = _make_em()
        callback = MagicMock()
        em.set_on_enabled_state_change(callback)
        em.mqtt_enabled_points = {1, 2}
        em.publish_enabled_state()
        callback.assert_called_once()
        callback.reset_mock()
        # Second call with the SAME set must not fire again.
        em.publish_enabled_state()
        callback.assert_not_called()

    def test_else_branch_also_updates_last_published_enabled_not_none(self):
        """The else branch (taken when the set is already unchanged) must
        also set _last_published_enabled to the real current set, not
        None — otherwise a THIRD call would see current != None and
        incorrectly fire the callback even though nothing ever changed."""
        em = _make_em()
        callback = MagicMock()
        em.set_on_enabled_state_change(callback)
        em.mqtt_enabled_points = {1, 2}
        em._last_published_enabled = frozenset({1, 2})  # already matches -> else branch
        em.publish_enabled_state()
        callback.assert_not_called()
        em.publish_enabled_state()  # still unchanged -> must still not fire
        callback.assert_not_called()


class TestSetOnEnabledStateChange(unittest.TestCase):
    """set_on_enabled_state_change stores the callback."""

    def test_setter_stores_callback(self):
        em = _make_em()
        cb = MagicMock()
        em.set_on_enabled_state_change(cb)
        self.assertIs(em._on_enabled_state_change, cb)


class TestResubscribeAll(unittest.TestCase):
    """resubscribe_all: re-subscribes all entity command topics, management
    topics, changelog topics, and dynamic map topics."""

    def _make_em_with_resubscribe(self):
        """EM with real resubscribe_all (not patched out)."""
        with patch('nibe_entity_manager.EntityManager._setup_history_loading'), \
             patch('nibe_entity_manager.EntityManager._setup_dynamic_map_loading'):
            from nibe_entity_manager import EntityManager
            em = EntityManager(
                api_client  = MagicMock(),
                publisher   = MagicMock(),
                notify_fn   = MagicMock(),
                dismiss_fn  = MagicMock(),
                mqtt_client = MagicMock(),
            )
        em.device_info = {}
        em.device_name = 'Test'
        # Wire up minimal callback stubs that resubscribe_all references
        em._on_history_message  = MagicMock()
        em._on_unread_message   = MagicMock()
        em._on_dynamic_map_message    = MagicMock()
        em._on_active_dynamic_message = MagicMock()
        return em

    def test_reassigns_value_cache_and_last_bulk_fetch_under_em_lock(self):
        """resubscribe_all() runs on paho's own MQTT network thread (via
        on_connect after a broker reconnect), concurrently with the poll
        loop thread reading/writing the same two attributes in
        update_all_states(). Regression test for a real race: reassigning
        value_cache/last_bulk_fetch used to be two plain, unsynchronized
        attribute writes. Proves actual mutual exclusion — not just that
        the final values happen to look right — by holding _em_lock on the
        main thread and confirming resubscribe_all() genuinely blocks on
        it from a background thread, rather than sailing through."""
        import threading
        import time as _time

        em = self._make_em_with_resubscribe()
        started = threading.Event()
        finished = threading.Event()

        def _run():
            started.set()
            em.resubscribe_all()
            finished.set()

        with em._em_lock:
            t = threading.Thread(target=_run)
            t.start()
            self.assertTrue(started.wait(timeout=5), "background thread never started")
            # Give resubscribe_all() ample opportunity to run if it were
            # NOT actually blocked on _em_lock — a real race here would
            # complete almost instantly since everything else it touches
            # (self.mqtt, callbacks) is mocked.
            _time.sleep(0.3)
            self.assertFalse(
                finished.is_set(),
                "resubscribe_all() completed while _em_lock was held by "
                "another thread — the value_cache/last_bulk_fetch "
                "reassignment is not actually serialized by the lock",
            )

        # Lock released — resubscribe_all() must now complete promptly.
        self.assertTrue(finished.wait(timeout=5),
                        "resubscribe_all() never completed after _em_lock was released")
        t.join(timeout=5)
        self.assertFalse(t.is_alive())

    def test_resubscribes_entity_command_topics(self):
        em = self._make_em_with_resubscribe()
        entity_info = {
            'point_id': 100, 'entity_type': 'switch',
            'command_topic': 'nibe/cmd/100',
            'availability_topic': 'nibe/avail/100',
            'state_topic': 'nibe/state/100',
        }
        em.active_entities_by_id[100] = entity_info
        em.resubscribe_all()
        em.mqtt.subscribe.assert_any_call('nibe/cmd/100', qos=1)

    def test_value_cache_reset_to_functional_instance(self):
        """value_cache must be reset to a genuinely usable ValueCache, not
        just something truthy — a mutation to None here would crash the
        very next should_publish() call on the following poll cycle with
        AttributeError, not just silently skip clearing stale state."""
        em = self._make_em_with_resubscribe()
        em.resubscribe_all()
        # Must not raise, and must behave like a fresh cache (first call
        # for a never-seen point always publishes).
        self.assertTrue(em.value_cache.should_publish(999, 42, threshold=1))

    def test_register_mgmt_subscription_appends_the_exact_triple(self):
        """register_mgmt_subscription() must append (topic, handler, qos)
        to _mgmt_subscriptions, not overwrite it or drop/reorder fields."""
        em = self._make_em_with_resubscribe()
        em._mgmt_subscriptions = []
        handler = MagicMock()
        em.register_mgmt_subscription('nibe/mgmt/force_poll', handler, qos=2)
        self.assertEqual(em._mgmt_subscriptions, [('nibe/mgmt/force_poll', handler, 2)])

    def test_register_mgmt_subscription_default_qos_is_one(self):
        """When qos is omitted, register_mgmt_subscription must default to
        QoS 1, not some other value."""
        em = self._make_em_with_resubscribe()
        em._mgmt_subscriptions = []
        handler = MagicMock()
        em.register_mgmt_subscription('nibe/mgmt/force_poll', handler)
        self.assertEqual(em._mgmt_subscriptions, [('nibe/mgmt/force_poll', handler, 1)])

    def test_resubscribes_management_topics(self):
        em = self._make_em_with_resubscribe()
        handler = MagicMock()
        em._mgmt_subscriptions = [('nibe/mgmt/aid_mode', handler, 1)]
        em.resubscribe_all()
        em.mqtt.subscribe.assert_any_call('nibe/mgmt/aid_mode', qos=1)

    def test_resubscribes_changelog_and_dynamic_topics(self):
        from nibe_mqtt_publisher import BrowserTopic
        em = self._make_em_with_resubscribe()
        em.resubscribe_all()
        subscribed = [c.args[0] for c in em.mqtt.subscribe.call_args_list]
        self.assertIn(BrowserTopic.CHANGELOG_HISTORY, subscribed)
        self.assertIn(BrowserTopic.CHANGELOG_UNREAD, subscribed)
        self.assertIn(BrowserTopic.DYNAMIC_MAP, subscribed)
        self.assertIn(BrowserTopic.ACTIVE_DYNAMIC, subscribed)

    def test_resubscribed_command_callback_dispatches_to_handle_command(self):
        """The MQTT command callback registered by resubscribe_all must invoke
        _handle_command when called (line 2300)."""
        em = self._make_em_with_resubscribe()
        cmd_topic = 'nibe/cmd/200'
        entity_info = {
            'point_id': 200, 'entity_type': 'switch',
            'command_topic': cmd_topic,
            'availability_topic': 'nibe/avail/200',
            'state_topic': 'nibe/state/200',
        }
        em.active_entities_by_id[200] = entity_info

        stored_cb = {}
        def fake_callback_add(topic, cb):
            stored_cb[topic] = cb
        em.mqtt.message_callback_add = MagicMock(side_effect=fake_callback_add)

        em.resubscribe_all()
        self.assertIn(cmd_topic, stored_cb)

        msg = MagicMock()
        msg.payload = b'1'
        with patch.object(em, '_handle_command') as mock_handle:
            stored_cb[cmd_topic](None, None, msg)
        # Must dispatch with the SAME entity_info bound in the closure and
        # the SAME message object — not e.g. None substituted for either
        # argument, and not a bare single-argument call.
        mock_handle.assert_called_once_with(entity_info, msg)

    def test_entity_without_command_topic_skipped(self):
        em = self._make_em_with_resubscribe()
        entity_info = {
            'point_id': 200, 'entity_type': 'sensor',
            'command_topic': None,
            'availability_topic': 'nibe/avail/200',
            'state_topic': 'nibe/state/200',
        }
        em.active_entities_by_id[200] = entity_info
        em.resubscribe_all()
        cmd_subs = [c for c in em.mqtt.subscribe.call_args_list
                    if c.args[0] == 'nibe/cmd/200']
        self.assertEqual(cmd_subs, [])

    def test_entity_without_command_topic_does_not_abort_remaining_entities(self):
        """The per-entity loop uses 'continue' to skip an entity with no
        command_topic, not 'break' — a 'break' would silently abandon every
        subsequent entity in the snapshot too. Order entity dicts so the
        skip-worthy one is iterated first (dict preserves insertion order)."""
        em = self._make_em_with_resubscribe()
        skip_entity = {
            'point_id': 1, 'entity_type': 'sensor',
            'command_topic': None,
            'availability_topic': 'nibe/avail/1',
            'state_topic': 'nibe/state/1',
        }
        keep_entity = {
            'point_id': 2, 'entity_type': 'switch',
            'command_topic': 'nibe/cmd/2',
            'availability_topic': 'nibe/avail/2',
            'state_topic': 'nibe/state/2',
        }
        em.active_entities_by_id[1] = skip_entity
        em.active_entities_by_id[2] = keep_entity
        em.resubscribe_all()
        subscribed_topics = [c.args[0] for c in em.mqtt.subscribe.call_args_list]
        self.assertIn('nibe/cmd/2', subscribed_topics,
                       "an entity after a skipped one must still be subscribed")

    def test_last_bulk_fetch_reset_to_zero(self):
        """last_bulk_fetch must be reset to the literal 0 (falsy timestamp
        meaning 'no successful fetch yet'), not None — the poll loop
        compares it numerically (time.time() - self.last_bulk_fetch), which
        would raise TypeError against None on the very next poll."""
        em = self._make_em_with_resubscribe()
        em.last_bulk_fetch = 999999.0
        em.resubscribe_all()
        self.assertEqual(em.last_bulk_fetch, 0)
        self.assertIsInstance(em.last_bulk_fetch, int)

    def test_final_log_reports_correct_entity_and_mgmt_counts(self):
        """The closing log_mqtt.info call reports entity_count and
        mgmt_count — these are accumulated by a plain += 1 per iteration
        rather than being derived from len(snapshot)/len(_mgmt_subscriptions)
        directly. Verify the exact counts logged against independently-known
        inputs: 2 entities with command topics (a 3rd has none and must not
        be counted) and 3 mgmt subscriptions."""
        em = self._make_em_with_resubscribe()
        em.active_entities_by_id[1] = {
            'point_id': 1, 'entity_type': 'switch', 'command_topic': 'nibe/cmd/1',
            'availability_topic': 'nibe/avail/1', 'state_topic': 'nibe/state/1',
        }
        em.active_entities_by_id[2] = {
            'point_id': 2, 'entity_type': 'switch', 'command_topic': 'nibe/cmd/2',
            'availability_topic': 'nibe/avail/2', 'state_topic': 'nibe/state/2',
        }
        em.active_entities_by_id[3] = {
            'point_id': 3, 'entity_type': 'sensor', 'command_topic': None,
            'availability_topic': 'nibe/avail/3', 'state_topic': 'nibe/state/3',
        }
        em._mgmt_subscriptions = [
            ('nibe/mgmt/a', MagicMock(), 1),
            ('nibe/mgmt/b', MagicMock(), 1),
            ('nibe/mgmt/c', MagicMock(), 0),
        ]
        with patch('nibe_entity_manager.log_mqtt') as mock_log:
            em.resubscribe_all()
        mock_log.info.assert_called_once()
        args = mock_log.info.call_args.args
        # args[0] is the format string; the trailing two positional args are
        # entity_count and mgmt_count per the source's log_mqtt.info(fmt, entity_count, mgmt_count).
        self.assertEqual(args[-2], 2, "entity_count must count only entities with a command_topic")
        self.assertEqual(args[-1], 3, "mgmt_count must equal the number of registered mgmt subscriptions")

    def test_mgmt_subscription_callback_registered_with_correct_handler(self):
        """Each management subscription's own handler must be passed to
        message_callback_add verbatim, paired with its own topic — not a
        mismatched or dropped topic/handler."""
        em = self._make_em_with_resubscribe()
        handler_a = MagicMock(name='handler_a')
        handler_b = MagicMock(name='handler_b')
        em._mgmt_subscriptions = [
            ('nibe/mgmt/a', handler_a, 1),
            ('nibe/mgmt/b', handler_b, 1),
        ]
        em.resubscribe_all()
        calls = {c.args[0]: c.args[1] for c in em.mqtt.message_callback_add.call_args_list}
        self.assertIs(calls['nibe/mgmt/a'], handler_a)
        self.assertIs(calls['nibe/mgmt/b'], handler_b)

    def test_changelog_and_dynamic_map_callbacks_registered_with_correct_topic_and_handler(self):
        """Each of the four retained-topic re-subscriptions (changelog
        history/unread, dynamic map, active dynamic) must pair its OWN
        topic with its OWN stored callback in message_callback_add — a
        mutation swapping in None for either argument, or the wrong topic
        constant, must be caught here."""
        from nibe_mqtt_publisher import BrowserTopic
        em = self._make_em_with_resubscribe()
        em.resubscribe_all()
        calls = {c.args[0]: c.args[1] for c in em.mqtt.message_callback_add.call_args_list}
        self.assertIs(calls[BrowserTopic.CHANGELOG_HISTORY], em._on_history_message)
        self.assertIs(calls[BrowserTopic.CHANGELOG_UNREAD], em._on_unread_message)
        self.assertIs(calls[BrowserTopic.DYNAMIC_MAP], em._on_dynamic_map_message)
        self.assertIs(calls[BrowserTopic.ACTIVE_DYNAMIC], em._on_active_dynamic_message)


class TestDisableEntityUsesDiscard(unittest.TestCase):
    """disable_entity must use discard() not remove() so a concurrent double-
    disable from different threads does not raise KeyError."""

    def test_discard_on_already_removed_does_not_raise(self):
        """Simulate the race: point already removed from mqtt_enabled_points
        by another thread before the second caller reaches the set operation."""
        em = _make_em()
        # Set up a minimal enabled entity
        entity_info = {
            'point_id': 100, 'entity_type': 'sensor', 'entity_id': 'nibe_100',
            'state_topic': 'nibe/state/100', 'availability_topic': 'nibe/avail/100',
            'command_topic': None, 'attributes_topic': None,
        }
        em.active_entities_by_id[100] = entity_info
        em.mqtt_enabled_points.add(100)
        # First disable succeeds normally
        em.disable_entity(100)
        self.assertNotIn(100, em.mqtt_enabled_points)
        # Second disable on a point not in the set must not raise
        em.disable_entity(100)   # would raise KeyError with .remove()

    def test_discard_is_used_not_remove(self):
        """Verify the implementation uses discard, not remove, by inspecting
        the source — a regression guard so this is never silently reverted."""
        import inspect

        from nibe_entity_manager import EntityManager
        # disable_entity is a thin _em_lock wrapper; the implementation
        # (and the discard call) lives in _disable_entity_locked.
        src = inspect.getsource(EntityManager._disable_entity_locked)
        self.assertNotIn('mqtt_enabled_points.remove', src,
            "_disable_entity_locked must use .discard() not .remove() to be thread-safe")
        self.assertIn('mqtt_enabled_points.discard', src)
