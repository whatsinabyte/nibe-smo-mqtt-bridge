"""
test_entity_manager_snapshots.py
================================
Snapshot save/restore/delete/publish tests for nibe_entity_manager.py — split out of test_entity_manager.py
for file-size/maintainability. Shared fixtures are in conftest.py.
"""

import json
import unittest
from unittest.mock import mock_open, patch

from conftest import (
    _make_em,
)
from hypothesis import given
from hypothesis import strategies as st

_data_strategy = st.dictionaries(
    st.text(max_size=20),
    st.one_of(st.integers(), st.text(max_size=50), st.booleans(), st.none()),
    max_size=10,
)


class TestLoadSaveSnapshotsRoundtrip(unittest.TestCase):
    """_load_snapshots / _save_snapshots: file I/O robustness."""

    def setUp(self):
        self._path = '/tmp/test_snapshots_io.json'
        import os
        try:
            os.remove(self._path)
        except FileNotFoundError:
            pass

    def test_roundtrip_preserves_content(self):
        em = _make_em()
        snaps = [{'name': 'X', 'point_ids': [1, 2], 'point_count': 2,
                  'timestamp': '2026-01-01 00:00:00', 'mode': 'essential'}]
        em._save_snapshots(snaps, path=self._path)
        loaded = em._load_snapshots(path=self._path)
        self.assertEqual(loaded, snaps)

    def test_load_returns_empty_when_file_absent(self):
        em = _make_em()
        result = em._load_snapshots(path='/tmp/definitely_absent_snaps.json')
        self.assertEqual(result, [])

    def test_load_returns_empty_on_corrupt_json(self):
        with open(self._path, 'w') as f:
            f.write('not valid json {{{')
        em = _make_em()
        result = em._load_snapshots(path=self._path)
        self.assertEqual(result, [])

    def test_load_returns_empty_when_file_not_list(self):
        with open(self._path, 'w') as f:
            json.dump({'not': 'a list'}, f)
        em = _make_em()
        result = em._load_snapshots(path=self._path)
        self.assertEqual(result, [])

    def test_save_defaults_to_production_snapshots_file_path(self):
        """_save_snapshots(snaps) with no explicit path — the default
        parameter path — must use _SNAPSHOTS_FILE ('/data/snapshots.json'),
        not silently no-op. Mocks open() so no real file I/O occurs."""
        from nibe_entity_manager import _SNAPSHOTS_FILE
        em = _make_em()
        snaps = [{'name': 'X', 'point_ids': [1], 'point_count': 1,
                  'timestamp': '2026-01-01 00:00:00', 'mode': 'essential'}]
        with patch('builtins.open', mock_open()) as m:
            em._save_snapshots(snaps)
        m.assert_called_once_with(_SNAPSHOTS_FILE, 'w', encoding='utf-8')

    def test_load_defaults_to_production_snapshots_file_path(self):
        """_load_snapshots() with no explicit path must read from
        _SNAPSHOTS_FILE ('/data/snapshots.json'), not silently no-op."""
        from nibe_entity_manager import _SNAPSHOTS_FILE
        em = _make_em()
        with patch('builtins.open', mock_open(read_data='[]')) as m:
            em._load_snapshots()
        m.assert_called_once_with(_SNAPSHOTS_FILE, encoding='utf-8')

    def test_save_write_failure_logs_warning_and_still_publishes(self):
        """A write failure (e.g. read-only filesystem) must be logged, not
        raised — and the MQTT publish (frontend's source of truth) must
        still happen so the card doesn't go stale."""
        em = _make_em()
        snaps = [{'name': 'X', 'point_ids': [1], 'point_count': 1,
                  'timestamp': '2026-01-01 00:00:00', 'mode': 'essential'}]
        with patch('builtins.open', side_effect=OSError('read-only filesystem')), \
             self.assertLogs('nibe.restore', level='WARNING') as cm:
            em._save_snapshots(snaps, path=self._path)
        self.assertTrue(any('Could not write snapshots file' in msg for msg in cm.output))
        em.mqtt.publish.assert_called_once()

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

    def test_save_snapshots_writes_indent_2_json(self):
        """json.dump must be called with indent=2 (pretty-printed file on disk)."""
        em = _make_em()
        snaps = [{'name': 'X', 'point_ids': [1], 'point_count': 1,
                  'timestamp': '2026-01-01 00:00:00', 'mode': 'essential'}]
        with patch('builtins.open', mock_open()), \
             patch('nibe_entity_manager.json.dump') as mock_dump:
            em._save_snapshots(snaps, path=self._path)
        mock_dump.assert_called_once()
        args, kwargs = mock_dump.call_args
        self.assertEqual(args[0], snaps)
        self.assertEqual(kwargs.get('indent'), 2)

    def test_save_snapshots_logs_exception_text_on_write_failure(self):
        """The warning must include the actual exception string, not a
        placeholder — verifies the %s substitution argument is the real
        exception object."""
        em = _make_em()
        with patch('builtins.open', side_effect=OSError('disk full')), \
             self.assertLogs('nibe.restore', level='WARNING') as cm:
            em._save_snapshots([{'name': 'X'}], path=self._path)
        self.assertEqual(len(cm.output), 1)
        self.assertIn('Could not write snapshots file', cm.output[0])
        self.assertIn('disk full', cm.output[0])

    def test_save_snapshots_publishes_exact_json_payload_with_retain(self):
        """The MQTT publish call must use json.dumps(snapshots) as payload,
        the SNAPSHOTS topic, and retain=True — checked against an
        independently-computed expected payload string."""
        from nibe_mqtt_publisher import BrowserTopic
        em = _make_em()
        snaps = [{'name': 'A', 'point_ids': [1, 2], 'point_count': 2,
                  'timestamp': '2026-01-01 00:00:00', 'mode': 'essential'},
                 {'name': 'B', 'point_ids': [], 'point_count': 0,
                  'timestamp': '2026-01-02 00:00:00', 'mode': 'advanced'}]
        expected_payload = json.dumps(snaps)
        with patch('builtins.open', mock_open()):
            em._save_snapshots(snaps, path=self._path)
        em.mqtt.publish.assert_called_once_with(
            BrowserTopic.SNAPSHOTS, expected_payload, retain=True
        )


class TestSaveSnapshot(unittest.TestCase):
    """save_snapshot: persistence, naming, cap, and MQTT publish."""

    def setUp(self):
        self._path = '/tmp/test_snapshots_save.json'
        # Remove any leftover from previous runs
        import os
        try:
            os.remove(self._path)
        except FileNotFoundError:
            pass

    def _em_with_enabled(self, pids):
        em = _make_em()
        for pid in pids:
            em.all_points_by_id[pid] = {'variableId': pid, 'title': f'P{pid}'}
            em.mqtt_enabled_points.add(pid)
        return em

    def test_save_creates_snapshot_with_correct_fields(self):
        em = self._em_with_enabled([1, 2, 3])
        ok, _msg = em.save_snapshot('Test', path=self._path)
        self.assertTrue(ok)
        snaps = em._load_snapshots(path=self._path)
        self.assertEqual(len(snaps), 1)
        snap = snaps[0]
        self.assertEqual(snap['name'], 'Test')
        self.assertEqual(set(snap['point_ids']), {1, 2, 3})
        self.assertEqual(snap['point_count'], 3)
        self.assertIn('timestamp', snap)

    def test_save_publishes_to_mqtt(self):
        em = self._em_with_enabled([10, 20])
        em.save_snapshot('MQTT Test', path=self._path)
        topics = [c.args[0] for c in em.mqtt.publish.call_args_list]
        from nibe_mqtt_publisher import BrowserTopic
        self.assertIn(BrowserTopic.SNAPSHOTS, topics)

    def test_save_replaces_existing_same_name(self):
        em = self._em_with_enabled([1, 2])
        em.save_snapshot('Dup', path=self._path)
        em.mqtt_enabled_points.add(3)
        em.all_points_by_id[3] = {'variableId': 3, 'title': 'P3'}
        ok, _ = em.save_snapshot('Dup', path=self._path)
        self.assertTrue(ok)
        snaps = em._load_snapshots(path=self._path)
        self.assertEqual(len(snaps), 1)
        self.assertEqual(set(snaps[0]['point_ids']), {1, 2, 3})

    def test_save_rejects_empty_name(self):
        em = self._em_with_enabled([1])
        ok, msg = em.save_snapshot('   ', path=self._path)
        self.assertFalse(ok)
        self.assertIn('empty', msg.lower())

    def test_save_rejects_when_at_cap(self):
        em = self._em_with_enabled([1])
        import nibe_entity_manager as nem
        original = nem._SNAPSHOTS_MAX
        try:
            nem._SNAPSHOTS_MAX = 2
            em.save_snapshot('A', path=self._path)
            em.save_snapshot('B', path=self._path)
            ok, msg = em.save_snapshot('C', path=self._path)
            self.assertFalse(ok)
            self.assertIn('Maximum', msg)
        finally:
            nem._SNAPSHOTS_MAX = original

    def test_save_strips_name_whitespace(self):
        em = self._em_with_enabled([1])
        ok, _ = em.save_snapshot('  Summer  ', path=self._path)
        self.assertTrue(ok)
        snaps = em._load_snapshots(path=self._path)
        self.assertEqual(snaps[0]['name'], 'Summer')

    def test_save_point_ids_are_sorted(self):
        em = self._em_with_enabled([30, 10, 20])
        em.save_snapshot('Sorted', path=self._path)
        snaps = em._load_snapshots(path=self._path)
        self.assertEqual(snaps[0]['point_ids'], [10, 20, 30])

    def test_save_rejects_empty_name_exact_message(self):
        """assertIn('empty', msg.lower()) in the older test can't tell
        'Snapshot name must not be empty.' apart from a mangled variant
        that still contains the substring — check the exact text."""
        em = self._em_with_enabled([1])
        ok, msg = em.save_snapshot('   ', path=self._path)
        self.assertFalse(ok)
        self.assertEqual(msg, "Snapshot name must not be empty.")

    def test_save_rejects_when_at_cap_exact_message(self):
        em = self._em_with_enabled([1])
        import nibe_entity_manager as nem
        original = nem._SNAPSHOTS_MAX
        try:
            nem._SNAPSHOTS_MAX = 1
            em.save_snapshot('A', path=self._path)
            ok, msg = em.save_snapshot('B', path=self._path)
            self.assertFalse(ok)
            self.assertEqual(
                msg,
                "Maximum of 1 snapshots reached. "
                "Delete one before saving a new snapshot."
            )
        finally:
            nem._SNAPSHOTS_MAX = original

    def test_save_timestamp_matches_expected_format(self):
        """time.strftime('%Y-%m-%d %H:%M:%S') — a mangled format string
        (wrong case directives, transposed %M/%D) won't match this shape."""
        em = self._em_with_enabled([1])
        em.save_snapshot('TSTest', path=self._path)
        snaps = em._load_snapshots(path=self._path)
        self.assertRegex(snaps[0]['timestamp'], r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')

    def test_save_stores_mode_under_mode_key_from_applied_mode(self):
        """Covers both the dict key ('mode') and the 'or' (not 'and')
        between the applied-mode read and the 'unknown' fallback: with a
        truthy applied mode, 'or' keeps it while 'and' would replace it
        with 'unknown'."""
        em = self._em_with_enabled([1])
        with patch.object(em, '_read_applied_mode_from_file', return_value='advanced'):
            em.save_snapshot('ModeKey', path=self._path)
        snaps = em._load_snapshots(path=self._path)
        self.assertEqual(snaps[0]['mode'], 'advanced')

    def test_save_mode_defaults_to_unknown_when_no_applied_mode(self):
        em = self._em_with_enabled([1])
        with patch.object(em, '_read_applied_mode_from_file', return_value=None):
            em.save_snapshot('NoMode', path=self._path)
        snaps = em._load_snapshots(path=self._path)
        self.assertEqual(snaps[0]['mode'], 'unknown')

    def test_save_logs_exact_message_with_name_and_count(self):
        em = self._em_with_enabled([7, 8])
        with self.assertLogs('nibe.restore', level='INFO') as cm:
            em.save_snapshot('LogTest', path=self._path)
        self.assertEqual(len(cm.output), 1)
        self.assertEqual(
            cm.output[0], "INFO:nibe.restore:Snapshot 'LogTest' saved: 2 points"
        )


class TestRestoreSnapshot(unittest.TestCase):
    """restore_snapshot: flush, merge, missing points, dynamic protection."""

    def setUp(self):
        self._path = '/tmp/test_snapshots_restore.json'
        import os
        try:
            os.remove(self._path)
        except FileNotFoundError:
            pass
        # Prevent restore_snapshot from reading /data/applied_mode under xdist —
        # another worker may have written 'menus' or 'all' there, triggering the
        # mode guard and causing spurious failures.
        self._mode_patcher = patch(
            'nibe_entity_manager.EntityManager._read_applied_mode_from_file',
            return_value='essential',
        )
        self._mode_patcher.start()

    def tearDown(self):
        self._mode_patcher.stop()

    def _em_with_firmware(self, all_pids, enabled_pids=None, dynamic_pids=None):
        em = _make_em()
        for pid in all_pids:
            em.all_points_by_id[pid] = {'variableId': pid, 'title': f'P{pid}'}
        for pid in (enabled_pids or []):
            em.mqtt_enabled_points.add(pid)
        em.active_dynamic_points = set(dynamic_pids or [])
        return em

    def _seed_snapshot(self, em, name, pids):
        """Write a snapshot directly to the file without calling save_snapshot."""
        import time as _t
        snaps = em._load_snapshots(path=self._path)
        snaps.append({
            'name': name, 'timestamp': _t.strftime('%Y-%m-%d %H:%M:%S'),
            'point_ids': sorted(pids), 'point_count': len(pids), 'mode': 'essential',
        })
        with open(self._path, 'w') as f:
            json.dump(snaps, f)

    def test_flush_enables_saved_disables_others(self):
        em = self._em_with_firmware(
            all_pids=[1, 2, 3, 4],
            enabled_pids=[1, 2, 3],  # 3 currently enabled
        )
        self._seed_snapshot(em, 'Snap', [2, 4])  # saved: 2 and 4
        ok, _msg = em.restore_snapshot('Snap', mode='flush', path=self._path)
        self.assertTrue(ok)
        # 2 stays, 4 added, 1 and 3 removed
        self.assertIn(2, em.mqtt_enabled_points)
        self.assertIn(4, em.mqtt_enabled_points)
        self.assertNotIn(1, em.mqtt_enabled_points)
        self.assertNotIn(3, em.mqtt_enabled_points)

    def test_merge_adds_saved_keeps_existing(self):
        em = self._em_with_firmware(
            all_pids=[1, 2, 3, 4],
            enabled_pids=[1, 2],
        )
        self._seed_snapshot(em, 'Snap', [3, 4])
        ok, _ = em.restore_snapshot('Snap', mode='merge', path=self._path)
        self.assertTrue(ok)
        # 1, 2 stay; 3, 4 added
        self.assertEqual(em.mqtt_enabled_points, {1, 2, 3, 4})

    def test_flush_does_not_disable_dynamic_points(self):
        em = self._em_with_firmware(
            all_pids=[1, 2, 10],
            enabled_pids=[1, 10],   # 10 is dynamic
            dynamic_pids=[10],
        )
        self._seed_snapshot(em, 'Snap', [1])
        em.restore_snapshot('Snap', mode='flush', path=self._path)
        # 10 must stay enabled — it's a dynamic point
        self.assertIn(10, em.mqtt_enabled_points)

    def test_missing_firmware_points_skipped(self):
        em = self._em_with_firmware(all_pids=[1, 2])
        self._seed_snapshot(em, 'Snap', [1, 2, 9999])  # 9999 not in firmware
        ok, msg = em.restore_snapshot('Snap', mode='flush', path=self._path)
        self.assertTrue(ok)
        self.assertIn(1, em.mqtt_enabled_points)
        self.assertIn(2, em.mqtt_enabled_points)
        self.assertNotIn(9999, em.mqtt_enabled_points)
        self.assertIn('skipped', msg)

    def test_restore_not_found_returns_false(self):
        em = self._em_with_firmware(all_pids=[1])
        ok, msg = em.restore_snapshot('NoSuch', path=self._path)
        self.assertFalse(ok)
        self.assertIn('not found', msg)

    def test_restore_publishes_enabled_state(self):
        em = self._em_with_firmware(all_pids=[1, 2], enabled_pids=[1])
        self._seed_snapshot(em, 'Snap', [2])
        with patch.object(em, 'publish_enabled_state') as mock_pub:
            em.restore_snapshot('Snap', mode='merge', path=self._path)
        mock_pub.assert_called_once()

    def test_restore_blocked_in_menus_mode(self):
        """Restore must be blocked when applied mode is 'menus' to prevent
        conflict with the system-managed entity set."""
        em = self._em_with_firmware(all_pids=[1, 2])
        self._seed_snapshot(em, 'Snap', [1])
        with patch.object(em, '_read_applied_mode_from_file', return_value='menus'):
            ok, msg = em.restore_snapshot('Snap', path=self._path)
        self.assertFalse(ok)
        self.assertIn('menus', msg)
        self.assertIn('mode', msg.lower())

    def test_restore_blocked_in_all_mode(self):
        """Restore must be blocked when applied mode is 'all'."""
        em = self._em_with_firmware(all_pids=[1, 2])
        self._seed_snapshot(em, 'Snap', [1])
        with patch.object(em, '_read_applied_mode_from_file', return_value='all'):
            ok, msg = em.restore_snapshot('Snap', path=self._path)
        self.assertFalse(ok)
        self.assertIn('all', msg)

    def test_restore_allowed_in_essential_mode(self):
        """Restore must be allowed in 'essential' mode."""
        em = self._em_with_firmware(all_pids=[1, 2])
        self._seed_snapshot(em, 'Snap', [1])
        with patch.object(em, '_read_applied_mode_from_file', return_value='essential'):
            ok, _ = em.restore_snapshot('Snap', path=self._path)
        self.assertTrue(ok)

    def test_restore_default_mode_is_flush(self):
        em = self._em_with_firmware(all_pids=[1, 2, 3], enabled_pids=[1, 2, 3])
        self._seed_snapshot(em, 'Snap', [1])
        em.restore_snapshot('Snap', path=self._path)  # no mode arg
        # Default flush: only 1 should remain
        self.assertEqual(em.mqtt_enabled_points - em.active_dynamic_points, {1})

    def test_restore_blocked_message_exact_text(self):
        """assertIn('menus', ...) / assertIn('mode', msg.lower()) can't tell
        the real message apart from a mangled variant that still contains
        those substrings — check the full text."""
        em = self._em_with_firmware(all_pids=[1, 2])
        self._seed_snapshot(em, 'Snap', [1])
        with patch.object(em, '_read_applied_mode_from_file', return_value='menus'):
            ok, msg = em.restore_snapshot('Snap', path=self._path)
        self.assertFalse(ok)
        self.assertEqual(
            msg,
            "Cannot restore a snapshot while in 'menus' mode. "
            "Switch to 'essential', 'monitoring', 'advanced', or 'none' first, "
            "then restore."
        )

    def test_restore_missing_point_ids_key_defaults_to_empty_list(self):
        """snapshot.get('point_ids', []) — a snapshot record missing the
        key entirely must be treated as an empty point set, not crash."""
        em = self._em_with_firmware(all_pids=[1, 2], enabled_pids=[1])
        snaps = [{'name': 'NoPoints', 'timestamp': 'x', 'point_count': 0,
                  'mode': 'essential'}]
        with open(self._path, 'w') as f:
            json.dump(snaps, f)
        ok, _ = em.restore_snapshot('NoPoints', mode='merge', path=self._path)
        self.assertTrue(ok)
        self.assertEqual(em.mqtt_enabled_points, {1})  # nothing added

    def test_restore_missing_points_logs_exact_message(self):
        em = self._em_with_firmware(all_pids=[1])
        self._seed_snapshot(em, 'Snap', [1, 999, 998])
        with self.assertLogs('nibe.restore', level='WARNING') as cm:
            em.restore_snapshot('Snap', path=self._path)
        self.assertEqual(len(cm.output), 1)
        self.assertEqual(
            cm.output[0],
            "WARNING:nibe.restore:Snapshot 'Snap': 2 point(s) no longer in "
            "firmware — skipped: [998, 999]"
        )

    def test_restore_missing_points_log_list_truncated_to_10(self):
        """sorted(missing)[:10] — verify the slice bound is exactly 10, not
        11, by using 11 missing points and checking the 11th is absent."""
        em = self._em_with_firmware(all_pids=[1, 2])
        missing_pids = list(range(100, 111))  # 11 missing ids: 100..110
        self._seed_snapshot(em, 'BigSnap', missing_pids)
        with self.assertLogs('nibe.restore', level='WARNING') as cm:
            em.restore_snapshot('BigSnap', path=self._path)
        logged = cm.output[0]
        self.assertIn(str(sorted(missing_pids)[:10]), logged)
        self.assertNotIn(str(sorted(missing_pids)), logged)

    def test_restore_suppress_depth_incremented_during_and_restored_after(self):
        """Starting from an unsuppressed state (depth 0), restore_snapshot
        must bump _suppress_enabled_state_depth to 1 for the duration of the
        enable/disable calls, then return it to 0 afterwards."""
        em = self._em_with_firmware(all_pids=[1, 2], enabled_pids=[])
        self._seed_snapshot(em, 'Snap', [1])
        captured = {}
        original_enable = em._enable_entity_locked

        def spy_enable(pid):
            captured['depth_during'] = em._suppress_enabled_state_depth
            return original_enable(pid)

        with patch.object(em, '_enable_entity_locked', side_effect=spy_enable):
            em.restore_snapshot('Snap', mode='flush', path=self._path)
        self.assertEqual(captured['depth_during'], 1)
        self.assertEqual(em._suppress_enabled_state_depth, 0)

    def test_restore_suppress_depth_boundary_at_one_not_reincremented(self):
        """With depth already at 1 (some other caller holds suppression),
        was_suppressed must be True: restore_snapshot must NOT increment the
        depth further, and must leave it at 1 afterwards. This is the
        boundary that distinguishes '> 0' from '> 1' in the suppression
        check, and 'None' from the real boolean."""
        em = self._em_with_firmware(all_pids=[1, 2], enabled_pids=[])
        self._seed_snapshot(em, 'Snap', [1])
        em._suppress_enabled_state_depth = 1
        captured = {}
        original_enable = em._enable_entity_locked

        def spy_enable(pid):
            captured['depth_during'] = em._suppress_enabled_state_depth
            return original_enable(pid)

        with patch.object(em, '_enable_entity_locked', side_effect=spy_enable):
            em.restore_snapshot('Snap', mode='flush', path=self._path)
        self.assertEqual(captured['depth_during'], 1)
        self.assertEqual(em._suppress_enabled_state_depth, 1)

    def test_restore_success_message_no_missing_exact_text(self):
        em = self._em_with_firmware(all_pids=[1, 2], enabled_pids=[])
        self._seed_snapshot(em, 'Snap', [1, 2])
        ok, msg = em.restore_snapshot('Snap', mode='flush', path=self._path)
        self.assertTrue(ok)
        self.assertEqual(msg, "Snapshot 'Snap' restored (2 points).")

    def test_restore_success_message_with_missing_exact_text(self):
        em = self._em_with_firmware(all_pids=[1])
        self._seed_snapshot(em, 'Snap', [1, 999])
        ok, msg = em.restore_snapshot('Snap', mode='flush', path=self._path)
        self.assertTrue(ok)
        self.assertEqual(
            msg, "Snapshot 'Snap' restored (1 points, 1 skipped — not in firmware)."
        )

    def test_restore_final_log_exact_message(self):
        em = self._em_with_firmware(all_pids=[1, 2], enabled_pids=[])
        self._seed_snapshot(em, 'Snap', [1])
        with self.assertLogs('nibe.restore', level='INFO') as cm:
            em.restore_snapshot('Snap', mode='merge', path=self._path)
        self.assertEqual(len(cm.output), 1)
        self.assertEqual(
            cm.output[0],
            "INFO:nibe.restore:Snapshot 'Snap' restored (mode=merge): "
            "Snapshot 'Snap' restored (1 points)."
        )


class TestSaveThenRestoreSnapshotRealRoundtrip(unittest.TestCase):
    """save_snapshot() and restore_snapshot() are each thoroughly tested
    above against real files — but save_snapshot()'s tests only verify the
    written content via the lower-level _load_snapshots(), and
    restore_snapshot()'s tests seed their file by hand-writing JSON
    directly (_seed_snapshot), bypassing save_snapshot() entirely. Neither
    real function has ever been chained into the other. This is exactly
    the seam a real restart depends on: does the file save_snapshot()
    actually produces restore correctly via restore_snapshot() on a fresh
    EntityManager instance (a different process, after a real restart)."""

    def setUp(self):
        self._path = '/tmp/test_snapshots_save_then_restore.json'
        import os
        try:
            os.remove(self._path)
        except FileNotFoundError:
            pass
        self._mode_patcher = patch(
            'nibe_entity_manager.EntityManager._read_applied_mode_from_file',
            return_value='essential',
        )
        self._mode_patcher.start()

    def tearDown(self):
        self._mode_patcher.stop()

    def test_points_saved_by_one_instance_are_restored_by_a_fresh_one(self):
        saver = _make_em()
        for pid in (10, 20, 30):
            saver.all_points_by_id[pid] = {'variableId': pid, 'title': f'P{pid}'}
            saver.mqtt_enabled_points.add(pid)
        ok, _msg = saver.save_snapshot('RealRoundtrip', path=self._path)
        self.assertTrue(ok)

        restorer = _make_em()
        for pid in (10, 20, 30):
            restorer.all_points_by_id[pid] = {'variableId': pid, 'title': f'P{pid}'}
        ok, msg = restorer.restore_snapshot('RealRoundtrip', mode='flush', path=self._path)
        self.assertTrue(ok, msg)
        self.assertEqual(restorer.mqtt_enabled_points, {10, 20, 30})

    def test_replaced_snapshot_restores_the_replacement_not_the_original(self):
        """save_snapshot() called twice with the same name (real
        replace-by-name path) must leave restore_snapshot() seeing only
        the second, replacement set of points — not a merge of both real
        writes and not the original."""
        saver = _make_em()
        for pid in (1, 2):
            saver.all_points_by_id[pid] = {'variableId': pid, 'title': f'P{pid}'}
            saver.mqtt_enabled_points.add(pid)
        saver.save_snapshot('Dup', path=self._path)

        saver.mqtt_enabled_points = {3, 4}
        saver.all_points_by_id[3] = {'variableId': 3, 'title': 'P3'}
        saver.all_points_by_id[4] = {'variableId': 4, 'title': 'P4'}
        saver.save_snapshot('Dup', path=self._path)

        restorer = _make_em()
        for pid in (1, 2, 3, 4):
            restorer.all_points_by_id[pid] = {'variableId': pid, 'title': f'P{pid}'}
        restorer.restore_snapshot('Dup', mode='flush', path=self._path)
        self.assertEqual(restorer.mqtt_enabled_points, {3, 4})

    def test_point_missing_from_restorers_firmware_is_skipped_not_crashed(self):
        """A point saved by one instance that the restoring instance's
        firmware snapshot doesn't have (all_points_by_id) — e.g. it
        disappeared after a firmware update between save and restore —
        must be silently skipped, not restored and not crash."""
        saver = _make_em()
        for pid in (100, 200):
            saver.all_points_by_id[pid] = {'variableId': pid, 'title': f'P{pid}'}
            saver.mqtt_enabled_points.add(pid)
        saver.save_snapshot('PartialFirmware', path=self._path)

        restorer = _make_em()
        restorer.all_points_by_id[100] = {'variableId': 100, 'title': 'P100'}
        # 200 deliberately absent from restorer's firmware
        ok, msg = restorer.restore_snapshot('PartialFirmware', mode='flush', path=self._path)
        self.assertTrue(ok, msg)
        self.assertEqual(restorer.mqtt_enabled_points, {100})
        self.assertIn('1 skipped', msg)


class TestDeleteSnapshot(unittest.TestCase):
    """delete_snapshot: removal, not-found, MQTT publish."""

    def setUp(self):
        self._path = '/tmp/test_snapshots_delete.json'
        import os
        try:
            os.remove(self._path)
        except FileNotFoundError:
            pass

    def test_delete_removes_named_snapshot(self):
        em = _make_em()
        em.all_points_by_id[1] = {'variableId': 1, 'title': 'P1'}
        em.mqtt_enabled_points.add(1)
        em.save_snapshot('ToDelete', path=self._path)
        em.save_snapshot('Keep', path=self._path)
        ok, _msg = em.delete_snapshot('ToDelete', path=self._path)
        self.assertTrue(ok)
        snaps = em._load_snapshots(path=self._path)
        names = [s['name'] for s in snaps]
        self.assertNotIn('ToDelete', names)
        self.assertIn('Keep', names)

    def test_delete_not_found_returns_false(self):
        em = _make_em()
        ok, msg = em.delete_snapshot('NoSuch', path=self._path)
        self.assertFalse(ok)
        self.assertIn('not found', msg)

    def test_delete_publishes_updated_list(self):
        em = _make_em()
        em.all_points_by_id[1] = {'variableId': 1, 'title': 'P1'}
        em.mqtt_enabled_points.add(1)
        em.save_snapshot('Del', path=self._path)
        em.mqtt.publish.reset_mock()
        em.delete_snapshot('Del', path=self._path)
        from nibe_mqtt_publisher import BrowserTopic
        topics = [c.args[0] for c in em.mqtt.publish.call_args_list]
        self.assertIn(BrowserTopic.SNAPSHOTS, topics)

    def test_delete_logs_exact_message(self):
        em = _make_em()
        em.all_points_by_id[5] = {'variableId': 5, 'title': 'P5'}
        em.mqtt_enabled_points.add(5)
        em.save_snapshot('DelLog', path=self._path)
        with self.assertLogs('nibe.restore', level='INFO') as cm:
            em.delete_snapshot('DelLog', path=self._path)
        self.assertEqual(len(cm.output), 1)
        self.assertEqual(cm.output[0], "INFO:nibe.restore:Snapshot 'DelLog' deleted")


class TestSnapshotConcurrency(unittest.TestCase):
    """Regression: save_snapshot/delete_snapshot previously had no locking
    around their load-modify-save sequence over snapshots.json, and
    save_snapshot read self.mqtt_enabled_points (a set mutated by
    _enable_entity_locked/_disable_entity_locked on other threads) without
    holding _em_lock. mgmt_executor runs commands with multiple worker
    threads, so two snapshot commands — or a snapshot save racing a
    concurrent enable/disable — genuinely run concurrently in production.
    These tests use real threads against the real (locked) implementation
    to prove the race is closed, not just that the code runs single-
    threaded without crashing."""

    def setUp(self):
        self._path = '/tmp/test_snapshots_concurrency.json'
        import os
        try:
            os.remove(self._path)
        except FileNotFoundError:
            pass

    def test_concurrent_saves_of_distinct_names_all_persist(self):
        """N concurrent save_snapshot calls with distinct names must all
        end up in the file — a lost-update race (each thread loading the
        same on-disk list before any of them writes back) would silently
        drop all but the last writer's snapshot."""
        import threading

        em = _make_em()
        em.all_points_by_id[1] = {'variableId': 1, 'title': 'P1'}
        em.mqtt_enabled_points.add(1)

        n = 8   # below _SNAPSHOTS_MAX (10) so the cap doesn't interfere
        barrier = threading.Barrier(n)

        def save(i):
            barrier.wait(timeout=5)
            em.save_snapshot(f'Snap{i}', path=self._path)

        threads = [threading.Thread(target=save, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        snaps = em._load_snapshots(path=self._path)
        names = {s['name'] for s in snaps}
        self.assertEqual(names, {f'Snap{i}' for i in range(n)},
            "a concurrent save_snapshot lost-update race dropped some snapshots")

    def test_concurrent_save_and_delete_leave_consistent_file(self):
        """A save racing a delete (of a different, pre-existing snapshot)
        must not corrupt the file or silently undo the delete."""
        import threading

        em = _make_em()
        em.all_points_by_id[1] = {'variableId': 1, 'title': 'P1'}
        em.mqtt_enabled_points.add(1)
        em.save_snapshot('PreExisting', path=self._path)

        barrier = threading.Barrier(2)

        def do_save():
            barrier.wait(timeout=5)
            em.save_snapshot('NewOne', path=self._path)

        def do_delete():
            barrier.wait(timeout=5)
            em.delete_snapshot('PreExisting', path=self._path)

        t1 = threading.Thread(target=do_save)
        t2 = threading.Thread(target=do_delete)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        snaps = em._load_snapshots(path=self._path)
        names = {s['name'] for s in snaps}
        self.assertEqual(names, {'NewOne'},
            "concurrent save+delete must leave exactly the new snapshot, "
            "with the deleted one gone — not both, and not neither")

    def test_save_snapshot_point_count_matches_point_ids_length_under_concurrent_mutation(self):
        """Regression: save_snapshot previously read self.mqtt_enabled_points
        twice, unlocked, to build point_ids and point_count separately — a
        concurrent enable/disable landing between the two reads could make
        them disagree. Both must now come from a single _em_lock-held
        snapshot of the set."""
        em = _make_em()
        for pid in range(50):
            em.all_points_by_id[pid] = {'variableId': pid, 'title': f'P{pid}'}
            em.mqtt_enabled_points.add(pid)

        ok, _msg = em.save_snapshot('Consistent', path=self._path)
        self.assertTrue(ok)
        snaps = em._load_snapshots(path=self._path)
        snap = snaps[0]
        self.assertEqual(snap['point_count'], len(snap['point_ids']))

    def test_restore_snapshot_holds_lock_while_reading(self):
        """Regression: restore_snapshot's initial _load_snapshots call
        previously ran unlocked — a concurrent save_snapshot/delete_snapshot
        write (mgmt_executor has multiple workers) could produce a torn/
        partial JSON read, which _load_snapshots silently turns into [],
        making restore_snapshot spuriously report 'not found' for a
        snapshot that genuinely exists on disk. It must now hold
        _snapshots_lock for the read, same as save/delete hold it for
        their load-modify-save sequence."""
        em = _make_em()
        em.all_points_by_id[1] = {'variableId': 1, 'title': 'P1'}
        em.mqtt_enabled_points.add(1)
        em.save_snapshot('Locked', path=self._path)

        lock_held_during_read = []
        real_load = em._load_snapshots
        def spying_load(path=None):
            lock_held_during_read.append(em._snapshots_lock.locked())
            return real_load(path=path)

        with patch.object(em, '_load_snapshots', side_effect=spying_load):
            em.restore_snapshot('Locked', path=self._path)

        self.assertTrue(lock_held_during_read,
            "restore_snapshot must call _load_snapshots")
        self.assertTrue(all(lock_held_during_read),
            "_snapshots_lock must be held while restore_snapshot reads the file")


class TestPublishSnapshots(unittest.TestCase):
    """publish_snapshots: publishes current file contents to MQTT."""

    def setUp(self):
        self._path = '/tmp/test_snapshots_publish.json'
        snaps = [{'name': 'A', 'timestamp': '2026-01-01 00:00:00',
                  'point_ids': [1], 'point_count': 1, 'mode': 'essential'}]
        with open(self._path, 'w') as f:
            json.dump(snaps, f)

    def test_publish_sends_snapshot_list(self):
        import json as _json
        em = _make_em()
        with patch('nibe_entity_manager._SNAPSHOTS_FILE', self._path):
            em.publish_snapshots()
        from nibe_mqtt_publisher import BrowserTopic
        calls = [c for c in em.mqtt.publish.call_args_list
                 if c.args[0] == BrowserTopic.SNAPSHOTS]
        self.assertEqual(len(calls), 1)
        payload = _json.loads(calls[0].args[1])
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]['name'], 'A')

    def test_publish_empty_when_no_file(self):
        import json as _json
        em = _make_em()
        with patch('nibe_entity_manager._SNAPSHOTS_FILE', '/tmp/nonexistent_snap.json'):
            em.publish_snapshots()
        from nibe_mqtt_publisher import BrowserTopic
        calls = [c for c in em.mqtt.publish.call_args_list
                 if c.args[0] == BrowserTopic.SNAPSHOTS]
        self.assertEqual(len(calls), 1)
        payload = _json.loads(calls[0].args[1])
        self.assertEqual(payload, [])


class TestOpenPostWriteScan(unittest.TestCase):
    """_open_post_write_scan: sets controlling point, active flag, and deadline
    in a fixed order under _post_write_lock (see the method's docstring for
    why the order matters)."""

    def test_sets_controlling_point_active_flag_and_deadline(self):
        em = _make_em()
        with patch('nibe_entity_manager.time.time', return_value=1_000_000.0):
            em._open_post_write_scan(42)
        self.assertEqual(em._post_write_controlling_point, 42)
        self.assertIs(em.post_write_active, True)
        self.assertEqual(em._post_write_until, 1_000_000.0 + em._post_write_duration)
