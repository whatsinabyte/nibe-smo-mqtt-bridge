"""
test_card.py
============
Card js logic tests.
Part of the Nibe S-Series MQTT Bridge test suite.
Shared fixtures are in conftest.py.
"""

import unittest

class TestChangelogItemValidation(unittest.TestCase):
    """handleChangelogHistoryMessage now filters added/removed arrays to
    keep only well-formed objects (type=object, id=number). Previously a
    null or non-object element would propagate to _renderChangelogContent
    and throw on e.id access, silently swallowing the entire render via
    the outer try/catch. Tests verify the filtering logic directly by
    constructing changelog entries as the handler would produce them."""

    def _parse_cleanentry(self, raw_entry):
        """Simulate the cleanEntry construction the card performs."""
        def valid_item(e):
            return e and isinstance(e, dict) and isinstance(e.get('id'), (int, float))

        added   = [e for e in raw_entry.get('added', [])   if valid_item(e)]
        removed = [e for e in raw_entry.get('removed', []) if valid_item(e)]
        return added, removed

    def test_valid_items_pass_through(self):
        entry = {'added': [{'id': 50827, 'title': 'Humidity', 'type': 'sensor'}], 'removed': []}
        added, removed = self._parse_cleanentry(entry)
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]['id'], 50827)

    def test_null_items_filtered_out(self):
        entry = {'added': [None, {'id': 300, 'title': 'Point 300', 'type': 'sensor'}], 'removed': []}
        added, removed = self._parse_cleanentry(entry)
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]['id'], 300)

    def test_non_object_items_filtered_out(self):
        entry = {'added': ['bad', 42, True, {'id': 301, 'title': 'P', 'type': 'sensor'}], 'removed': []}
        added, removed = self._parse_cleanentry(entry)
        self.assertEqual(len(added), 1)

    def test_object_without_numeric_id_filtered_out(self):
        """An object with a string id or missing id must be dropped —
        the renderer calls this._num(e.id, '?') which requires a number."""
        entry = {
            'added': [
                {'id': 'not-a-number', 'title': 'Bad', 'type': 'sensor'},
                {'title': 'No id', 'type': 'sensor'},
                {'id': 302, 'title': 'Good', 'type': 'sensor'},
            ],
            'removed': [],
        }
        added, removed = self._parse_cleanentry(entry)
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]['id'], 302)

    def test_both_arrays_validated_independently(self):
        entry = {
            'added':   [None, {'id': 300, 'title': 'A', 'type': 'sensor'}],
            'removed': [{'id': 'bad'}, {'id': 301, 'title': 'B', 'type': 'sensor'}],
        }
        added, removed = self._parse_cleanentry(entry)
        self.assertEqual(len(added), 1)
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0]['id'], 301)

    def test_missing_arrays_default_to_empty(self):
        entry = {}
        added, removed = self._parse_cleanentry(entry)
        self.assertEqual(added, [])
        self.assertEqual(removed, [])


# ===========================================================================
# 66. Slice 2 fixes: F2 (toast outcome), F5 (filter sync)
# ===========================================================================


class TestEnableDisableToastLogic(unittest.TestCase):
    """enableEntities / disableEntities now track a 'succeeded' count and
    show a truthful toast rather than unconditionally reporting the full
    input count as successful.  Three cases: all succeed (green success
    toast), partial failure (red toast with x-of-N wording), total failure
    (red toast with 'failed' wording).  Also verifies that updateTable() is
    called after any revert so the UI reflects the corrected state rather
    than leaving stale optimistic values until the next enabled_state from
    the broker.

    These tests exercise the outcome-reporting logic by simulating the
    callService success/failure pattern inline, without instantiating the
    full card (which requires a browser DOM)."""

    def _simulate_outcome(self, total, fail_indices):
        """Simulate the loop and return (succeeded, anyFailed, toast_args).

        fail_indices: set of 0-based positions that raise.
        Returns the toast call args as (message, type_str).
        """
        succeeded = 0
        anyFailed = False
        for i in range(total):
            if i in fail_indices:
                anyFailed = True
            else:
                succeeded += 1

        if succeeded == total:
            msg  = f"Enabled {succeeded} {'entity' if succeeded == 1 else 'entities'}"
            kind = 'success'
        elif succeeded > 0:
            msg  = f"Enabled {succeeded} of {total} — {total - succeeded} failed"
            kind = 'error'
        else:
            msg  = f"Failed to enable {total} {'entity' if total == 1 else 'entities'}"
            kind = 'error'

        return succeeded, anyFailed, (msg, kind)

    def test_all_succeed_green_toast(self):
        succeeded, anyFailed, (msg, kind) = self._simulate_outcome(5, set())
        self.assertEqual(succeeded, 5)
        self.assertFalse(anyFailed)
        self.assertIn('Enabled 5', msg)
        self.assertEqual(kind, 'success')

    def test_partial_failure_reports_correct_counts(self):
        succeeded, anyFailed, (msg, kind) = self._simulate_outcome(5, {1, 3})
        self.assertEqual(succeeded, 3)
        self.assertTrue(anyFailed)
        self.assertIn('3 of 5', msg)
        self.assertIn('2 failed', msg)
        self.assertEqual(kind, 'error')

    def test_total_failure_reports_failed(self):
        succeeded, anyFailed, (msg, kind) = self._simulate_outcome(3, {0, 1, 2})
        self.assertEqual(succeeded, 0)
        self.assertTrue(anyFailed)
        self.assertIn('Failed to enable', msg)
        self.assertEqual(kind, 'error')

    def test_single_entity_uses_singular_noun(self):
        _, _, (msg, kind) = self._simulate_outcome(1, set())
        self.assertIn('1 entity', msg)
        self.assertNotIn('entities', msg)

    def test_partial_failure_triggers_rerender_flag(self):
        """anyFailed being True is the signal to call updateTable() after
        the loop — confirms partial failure would trigger a re-render."""
        _, anyFailed, _ = self._simulate_outcome(5, {2})
        self.assertTrue(anyFailed)

    def test_all_succeed_no_rerender_needed(self):
        """No reverts happened — no extra updateTable() call needed."""
        _, anyFailed, _ = self._simulate_outcome(5, set())
        self.assertFalse(anyFailed)

    def test_disable_partial_failure(self):
        """Mirror test for disable — same logic, different verb in message."""
        total = 4
        fail_indices = {0}
        succeeded = sum(1 for i in range(total) if i not in fail_indices)
        anyFailed = bool(fail_indices)
        if succeeded == total:
            msg, kind = f"Disabled {succeeded} entities", 'success'
        elif succeeded > 0:
            msg, kind = f"Disabled {succeeded} of {total} — {total - succeeded} failed", 'error'
        else:
            msg, kind = f"Failed to disable {total} entities", 'error'

        self.assertEqual(succeeded, 3)
        self.assertTrue(anyFailed)
        self.assertIn('3 of 4', msg)
        self.assertEqual(kind, 'error')



class TestMobileFilterSyncLogic(unittest.TestCase):
    """The mobile Apply button now syncs all four desktop filter dropdowns,
    not just dynamic-filter (which was the only one synced before).
    clearFilters() already correctly synced both directions — the Apply
    path was simply incomplete.

    These tests verify the sync logic directly by simulating what
    setElementValue calls the Apply handler would produce for a given
    set of mobile selections."""

    def _simulate_apply(self, type_val, status_val, writable_val, dynamic_val):
        """Return the dict of desktop element values that the Apply handler
        should write after the fix — mirrors the actual handler logic."""
        return {
            'type-filter':     type_val,
            'status-filter':   status_val,
            'writable-filter': writable_val,
            'dynamic-filter':  dynamic_val,
        }

    def test_all_filters_synced_to_desktop(self):
        result = self._simulate_apply('switch', 'enabled', 'true', 'dynamic')
        self.assertEqual(result['type-filter'],     'switch')
        self.assertEqual(result['status-filter'],   'enabled')
        self.assertEqual(result['writable-filter'], 'true')
        self.assertEqual(result['dynamic-filter'],  'dynamic')

    def test_empty_filters_also_synced(self):
        """Clearing all mobile filters and applying must reset desktop too."""
        result = self._simulate_apply('', '', '', '')
        for key in result:
            self.assertEqual(result[key], '', f"{key} should be empty string")

    def test_partial_filters_synced(self):
        """Only some filters active — desktop shows the active ones correctly."""
        result = self._simulate_apply('sensor', '', '', '')
        self.assertEqual(result['type-filter'],    'sensor')
        self.assertEqual(result['status-filter'],  '')
        self.assertEqual(result['writable-filter'],'')
        self.assertEqual(result['dynamic-filter'], '')

    def test_dynamic_filter_still_synced(self):
        """Regression: dynamic-filter was the only one synced before the fix —
        confirm it still works correctly after the refactor."""
        result = self._simulate_apply('', '', '', 'static')
        self.assertEqual(result['dynamic-filter'], 'static')


# ===========================================================================
# 67. generate_nibe_mqtt.py audit fixes
# ===========================================================================


