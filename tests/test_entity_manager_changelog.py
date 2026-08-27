"""
test_entity_manager_changelog.py
================================
Changelog and dynamic-point history tests for nibe_entity_manager.py — split out of test_entity_manager.py
for file-size/maintainability. Shared fixtures are in conftest.py.
"""

import json
import time
import unittest
from collections import deque
from unittest.mock import MagicMock

from conftest import (
    _make_em,
)
from hypothesis import given
from hypothesis import strategies as st


class TestPruneChangelog(unittest.TestCase):
    def setUp(self):
        self.em = _make_em()
        self.em._last_prune_time = 0.0

    def _entry(self, age_days=0):
        return {
            "timestamp": time.time() - age_days * 86400,
            "iso_timestamp": "2024-01-01",
            "added": [],
            "removed": [],
        }

    def test_runs_when_due(self):
        self.assertTrue(self.em._prune_changelog_if_due())

    def test_skipped_when_recent(self):
        self.em._prune_changelog_if_due()
        self.assertFalse(self.em._prune_changelog_if_due())

    def test_old_entries_removed(self):
        from nibe_entity_manager import _CHANGELOG_MIN_ENTRIES

        self.em.changelog_retention_days = 30
        # Use more recent entries than the floor so the floor does not
        # confound the result.  All entries beyond the floor+recent count
        # should be evicted.
        n_recent = _CHANGELOG_MIN_ENTRIES + 5  # safely above the floor
        n_old = 10
        recent = [self._entry(1) for _ in range(n_recent)]
        old = [self._entry(60) for _ in range(n_old)]
        self.em.change_history = deque(recent + old, maxlen=500)
        self.em._prune_changelog_if_due()
        self.assertEqual(len(self.em.change_history), n_recent)

    def test_floor_preserved(self):
        from nibe_entity_manager import _CHANGELOG_MIN_ENTRIES

        self.em.changelog_retention_days = 1
        self.em.change_history = deque(
            [self._entry(10) for _ in range(_CHANGELOG_MIN_ENTRIES + 5)], maxlen=500
        )
        self.em._prune_changelog_if_due()
        self.assertEqual(len(self.em.change_history), _CHANGELOG_MIN_ENTRIES)

    def test_invalid_entries_dropped(self):
        self.em.change_history = deque([self._entry(1), {"not": "valid"}, "not a dict"], maxlen=500)
        self.em._prune_changelog_if_due()
        self.assertEqual(len(self.em.change_history), 1)

    def test_partially_malformed_entries_dropped(self):
        """The validity check requires ALL FOUR of added/removed/timestamp/
        iso_timestamp to be present (AND, not OR) — an entry missing just
        one of them must still be dropped. The existing 'invalid entries'
        test above uses entries missing EVERY field, which can't
        distinguish and/or: both connectives agree when all operands are
        false. These entries have some-but-not-all fields to actually
        exercise that distinction."""
        # Has added/removed/timestamp but not iso_timestamp — distinguishes
        # a mutation of the final `and` to `or` in the validity check.
        missing_timestamps = {"added": [1], "removed": [], "timestamp": 1.0}
        missing_added_removed = {"timestamp": 1.0, "iso_timestamp": "x"}  # no added/removed
        self.em.change_history = deque(
            [self._entry(1), missing_timestamps, missing_added_removed], maxlen=500
        )
        self.em._prune_changelog_if_due()
        self.assertEqual(len(self.em.change_history), 1)

    def test_maxlen_preserved(self):
        ml = self.em.change_history.maxlen
        self.em.change_history = deque([self._entry()], maxlen=ml)
        self.em._prune_changelog_if_due()
        self.assertEqual(self.em.change_history.maxlen, ml)

    def test_all_recent_kept(self):
        self.em.changelog_retention_days = 90
        self.em.change_history = deque([self._entry(1) for _ in range(10)], maxlen=500)
        self.em._prune_changelog_if_due()
        self.assertEqual(len(self.em.change_history), 10)

    def test_empty_does_not_crash(self):
        self.em.change_history = deque(maxlen=500)
        self.em._prune_changelog_if_due()
        self.assertEqual(len(self.em.change_history), 0)


class TestPruneChangelogProperties(unittest.TestCase):
    """Hypothesis properties for _prune_changelog_if_due."""

    def _make_fresh_em(self):
        em = _make_em()
        em._last_prune_time = 0.0
        return em

    def _add_entries(self, em, n, age_days=0):
        """Add n valid entries to change_history with given age in days."""
        import time as _time

        ts = _time.time() - age_days * 86400
        for i in range(n):
            em.change_history.appendleft(
                {
                    "timestamp": ts,
                    "iso_timestamp": "2020-01-01 00:00:00",
                    "added": [],
                    "removed": [],
                    "id": f"e{i}",
                    "unread": True,
                    "source": "test",
                    "triggered_by": None,
                }
            )

    @given(st.integers(min_value=1, max_value=200))
    def test_always_keeps_at_least_50_entries(self, n_entries):
        """After pruning, at least min(50, original) entries always remain."""
        from nibe_entity_manager import _CHANGELOG_MIN_ENTRIES

        em = self._make_fresh_em()
        self._add_entries(em, n_entries, age_days=9999)
        em._prune_changelog_if_due()
        expected_min = min(_CHANGELOG_MIN_ENTRIES, n_entries)
        self.assertGreaterEqual(len(em.change_history), expected_min)

    @given(st.integers(min_value=51, max_value=200))
    def test_old_entries_beyond_50_are_pruned(self, n_entries):
        """Entries older than retention_days (beyond the 50-entry floor) are removed."""
        from nibe_entity_manager import _CHANGELOG_MIN_ENTRIES

        em = self._make_fresh_em()
        em.changelog_retention_days = 1
        self._add_entries(em, n_entries, age_days=999)
        result = em._prune_changelog_if_due()
        self.assertTrue(result, "Expected prune to run")
        self.assertEqual(len(em.change_history), _CHANGELOG_MIN_ENTRIES)

    @given(st.integers(min_value=1, max_value=100))
    def test_recent_entries_never_pruned(self, n_entries):
        """Entries within retention period must never be pruned."""
        em = self._make_fresh_em()
        em.changelog_retention_days = 90
        self._add_entries(em, n_entries, age_days=1)
        em._prune_changelog_if_due()
        self.assertEqual(len(em.change_history), n_entries)

    def test_returns_false_when_called_too_soon(self):
        """Second call within _CHANGELOG_PRUNE_S returns False."""
        em = self._make_fresh_em()
        em._prune_changelog_if_due()  # first call — runs
        result = em._prune_changelog_if_due()  # too soon
        self.assertFalse(result)

    def test_returns_true_when_due(self):
        em = self._make_fresh_em()
        result = em._prune_changelog_if_due()
        self.assertTrue(result)

    @given(st.integers(min_value=0, max_value=200))
    def test_never_raises(self, n_entries):
        em = self._make_fresh_em()
        self._add_entries(em, n_entries, age_days=100)
        em._prune_changelog_if_due()  # must not raise


class TestMarkChangelogReadProperties(unittest.TestCase):
    """Hypothesis properties for mark_changelog_read."""

    def _make_fresh_em(self):
        return _make_em()

    def _add_unread(self, em, n):
        import time as _time

        for i in range(n):
            em.change_history.appendleft(
                {
                    "timestamp": _time.time(),
                    "iso_timestamp": "2020-01-01 00:00:00",
                    "added": [],
                    "removed": [],
                    "id": f"e{i}",
                    "unread": True,
                    "source": "test",
                    "triggered_by": None,
                }
            )

    @given(st.integers(min_value=0, max_value=50))
    def test_all_entries_marked_unread_false(self, n):
        """After mark_changelog_read, all entries have unread=False."""
        em = self._make_fresh_em()
        self._add_unread(em, n)
        em.mark_changelog_read()
        for entry in em.change_history:
            self.assertFalse(entry["unread"])

    @given(st.integers(min_value=0, max_value=50))
    def test_seq_incremented(self, n):
        """_history_seq must increment by at least 1."""
        em = self._make_fresh_em()
        self._add_unread(em, n)
        seq_before = em._history_seq
        em.mark_changelog_read()
        self.assertGreater(em._history_seq, seq_before)

    @given(st.integers(min_value=0, max_value=50))
    def test_publishes_zero_unread_count(self, n):
        """CHANGELOG_UNREAD publish must have unread_count=0."""
        import json as _json

        from nibe_mqtt_publisher import BrowserTopic

        em = self._make_fresh_em()
        self._add_unread(em, n)
        em.mark_changelog_read()
        calls = [
            c for c in em.mqtt.publish.call_args_list if c.args[0] == BrowserTopic.CHANGELOG_UNREAD
        ]
        self.assertTrue(calls)
        payload = _json.loads(calls[-1].args[1])
        self.assertEqual(payload["unread_count"], 0)

    @given(st.integers(min_value=0, max_value=50))
    def test_never_raises(self, n):
        em = self._make_fresh_em()
        self._add_unread(em, n)
        em.mark_changelog_read()  # must not raise

    @given(st.integers(min_value=1, max_value=50))
    def test_last_published_seq_updated(self, n):
        """_last_published_seq must match _history_seq after mark_changelog_read."""
        em = self._make_fresh_em()
        self._add_unread(em, n)
        em.mark_changelog_read()
        self.assertEqual(em._last_published_seq, em._history_seq)


class TestUpdateChangelogHistoryProperties(unittest.TestCase):
    """Hypothesis properties for _update_changelog_history."""

    _event_strategy = st.fixed_dictionaries(
        {
            "added": st.lists(st.integers(min_value=1, max_value=9999), max_size=5),
            "removed": st.lists(st.integers(min_value=1, max_value=9999), max_size=5),
            "source": st.sampled_from(["firmware", "user", "bridge", "api"]),
        }
    )

    @given(_event_strategy)
    def test_new_entry_always_unread(self, event):
        """Every new changelog entry must have unread=True."""
        em = _make_em()
        em._update_changelog_history(event)
        self.assertTrue(em.change_history[0]["unread"])

    @given(_event_strategy)
    def test_new_entry_id_starts_with_change(self, event):
        """Entry id must always start with 'change_'."""
        em = _make_em()
        em._update_changelog_history(event)
        self.assertTrue(em.change_history[0]["id"].startswith("change_"))

    @given(_event_strategy)
    def test_new_entry_is_first_in_history(self, event):
        """New entry must always be prepended (appendleft)."""
        em = _make_em()
        em._update_changelog_history(event)
        first = em.change_history[0]
        self.assertEqual(first["source"], event["source"])

    def test_provided_timestamp_is_preserved_not_overridden(self):
        """A caller-provided 'timestamp' must be used as-is — .get('timestamp',
        time.time()) only falls back to the current time when the key is
        absent. Previously untested: the Hypothesis strategy here never
        included a 'timestamp' key, so a mutation ignoring the provided
        value and always using time.time() went uncaught."""
        em = _make_em()
        provided_ts = 1_600_000_000.0
        event = {"added": [1], "removed": [], "source": "firmware", "timestamp": provided_ts}
        em._update_changelog_history(event)
        self.assertEqual(em.change_history[0]["timestamp"], provided_ts)

    def test_provided_iso_timestamp_is_preserved_not_overridden(self):
        em = _make_em()
        event = {
            "added": [1],
            "removed": [],
            "source": "firmware",
            "iso_timestamp": "2020-01-01T00:00:00",
        }
        em._update_changelog_history(event)
        self.assertEqual(em.change_history[0]["iso_timestamp"], "2020-01-01T00:00:00")

    @given(_event_strategy)
    def test_seq_always_increments(self, event):
        em = _make_em()
        seq_before = em._history_seq
        em._update_changelog_history(event)
        self.assertGreater(em._history_seq, seq_before)

    @given(_event_strategy)
    def test_added_preserved_exactly(self, event):
        em = _make_em()
        em._update_changelog_history(event)
        self.assertEqual(em.change_history[0]["added"], event["added"])

    @given(_event_strategy)
    def test_removed_preserved_exactly(self, event):
        em = _make_em()
        em._update_changelog_history(event)
        self.assertEqual(em.change_history[0]["removed"], event["removed"])

    @given(_event_strategy)
    def test_source_preserved(self, event):
        em = _make_em()
        em._update_changelog_history(event)
        self.assertEqual(em.change_history[0]["source"], event["source"])

    @given(_event_strategy)
    def test_default_source_is_firmware(self, event):
        """When source is absent, defaults to 'firmware'."""
        em = _make_em()
        event_no_source = {k: v for k, v in event.items() if k != "source"}
        em._update_changelog_history(event_no_source)
        self.assertEqual(em.change_history[0]["source"], "firmware")

    @given(_event_strategy)
    def test_default_added_is_empty_list(self, event):
        """When added is absent, defaults to []."""
        em = _make_em()
        event_no_added = {k: v for k, v in event.items() if k != "added"}
        em._update_changelog_history(event_no_added)
        self.assertEqual(em.change_history[0]["added"], [])

    @given(_event_strategy)
    def test_history_length_increases(self, event):
        """History length must increase by 1 (unless at maxlen)."""
        em = _make_em()
        before = len(em.change_history)
        em._update_changelog_history(event)
        self.assertEqual(len(em.change_history), before + 1)

    @given(_event_strategy)
    def test_unread_count_in_payload_matches_history(self, event):
        """unread_count in CHANGELOG_HISTORY payload must match actual count."""
        import json as _json

        from nibe_entity_manager import _decompress_payload
        from nibe_mqtt_publisher import BrowserTopic

        em = _make_em()
        em._update_changelog_history(event)
        calls = [
            c for c in em.mqtt.publish.call_args_list if c.args[0] == BrowserTopic.CHANGELOG_HISTORY
        ]
        self.assertTrue(calls)
        raw = _decompress_payload(calls[-1].args[1])
        payload = _json.loads(raw)
        actual_unread = sum(1 for e in em.change_history if e.get("unread", False))
        self.assertEqual(payload["unread_count"], actual_unread)

    @given(_event_strategy)
    def test_never_raises(self, event):
        em = _make_em()
        em._update_changelog_history(event)  # must not raise


class TestUpdateChangelogHistoryDefaultsAndPayload(unittest.TestCase):
    """Targeted (non-Hypothesis) tests for default values and MQTT payload
    structure of _update_changelog_history, closing survivors the property
    tests above don't reach: default 'iso_timestamp'/'removed' when the key
    is absent from the event, the 'id' timestamp scaling, exact-by-one seq
    increments, unread-count default semantics, and dict-key/retain
    fidelity of both published payloads."""

    def test_default_iso_timestamp_used_when_absent(self):
        """When 'iso_timestamp' is absent from the event, the entry must get
        a real formatted timestamp (_fmt_ts()), not None. A mutant changing
        the default to None would go uncaught by the 'preserved when
        provided' test, since that test always supplies the key."""
        em = _make_em()
        event = {"added": [1], "removed": [], "source": "firmware"}
        em._update_changelog_history(event)
        iso_ts = em.change_history[0]["iso_timestamp"]
        self.assertIsInstance(iso_ts, str)
        self.assertTrue(len(iso_ts) > 0)

    def test_default_removed_is_empty_list_when_absent(self):
        """Mirror of test_default_added_is_empty_list for 'removed' — not
        covered by the Hypothesis suite above, which always includes
        'removed' via the fixed_dictionaries strategy."""
        em = _make_em()
        event = {"added": [1], "source": "firmware"}
        em._update_changelog_history(event)
        self.assertEqual(em.change_history[0]["removed"], [])

    def test_id_computed_from_time_times_1000(self):
        """'id' must be f"change_{int(time.time() * 1000)}" — verified
        against an independently-mocked, fixed time.time() value rather
        than read back from the entry itself. Catches both '/1000' and
        '*1001' scaling mutants."""
        from unittest.mock import patch as _patch

        em = _make_em()
        fixed_time = 1_700_000_123.456
        with _patch("nibe_entity_manager.time.time", return_value=fixed_time):
            em._update_changelog_history({"added": [], "removed": [], "source": "firmware"})
        expected_id = f"change_{int(fixed_time * 1000)}"
        self.assertEqual(em.change_history[0]["id"], expected_id)

    def test_seq_increments_by_exactly_one(self):
        """Distinguishes '+= 1' from '+= 2' — starting from a non-zero seq
        so the two can't coincidentally agree."""
        em = _make_em()
        em._history_seq = 100
        em._update_changelog_history({"added": [], "removed": [], "source": "firmware"})
        self.assertEqual(em._history_seq, 101)

    def test_unread_count_excludes_entries_missing_unread_key(self):
        """unread_count = sum(1 for e in change_history if e.get('unread',
        False)). An entry with NO 'unread' key at all must not be counted —
        this exercises the 'default=True' mutant, which would incorrectly
        count such entries as unread. (A default of None is unreachable via
        black-box testing here since None is falsy exactly like False.)"""
        em = _make_em()
        em.change_history.appendleft(
            {"timestamp": 1.0, "iso_timestamp": "x", "added": [], "removed": []}
        )  # no 'unread' key
        em._update_changelog_history({"added": [], "removed": [], "source": "firmware"})
        # Only the freshly-appended entry (always unread=True) should count.
        from nibe_entity_manager import _decompress_payload

        history_calls = [c for c in em.mqtt.publish.call_args_list if "history" in c[0][0]]
        payload = json.loads(_decompress_payload(history_calls[-1].args[1]))
        self.assertEqual(payload["unread_count"], 1)

    def test_history_payload_has_all_expected_keys_with_correct_values(self):
        """Every key of history_payload (history/total_entries/unread_count/
        last_updated/_seq) must survive under its real name with the right
        value — catches key-rename mutants that the narrower per-field
        Hypothesis checks above don't target."""
        from nibe_entity_manager import _decompress_payload

        em = _make_em()
        em._history_seq = 7
        em._update_changelog_history({"added": [1], "removed": [], "source": "firmware"})
        history_calls = [c for c in em.mqtt.publish.call_args_list if "history" in c[0][0]]
        payload = json.loads(_decompress_payload(history_calls[-1].args[1]))
        self.assertEqual(len(payload["history"]), len(em.change_history))
        self.assertEqual(payload["total_entries"], len(em.change_history))
        self.assertEqual(payload["unread_count"], 1)
        self.assertIn("last_updated", payload)
        self.assertEqual(payload["_seq"], 8)

    def test_changelog_history_publish_topic_and_retain(self):
        """The CHANGELOG_HISTORY publish call must use the real topic and
        retain=True (not False/None/omitted)."""
        from nibe_entity_manager import BrowserTopic

        em = _make_em()
        em._update_changelog_history({"added": [], "removed": [], "source": "firmware"})
        calls = [
            c for c in em.mqtt.publish.call_args_list if c.args[0] == BrowserTopic.CHANGELOG_HISTORY
        ]
        self.assertTrue(calls)
        call = calls[-1]
        retain = call.kwargs.get("retain", call.args[2] if len(call.args) > 2 else None)
        self.assertIs(retain, True)

    def test_changelog_unread_publish_topic_payload_and_retain(self):
        """The CHANGELOG_UNREAD publish call must use the real topic, a
        parseable payload with the correct unread_count and a present
        'last_change' key, and retain=True."""
        from nibe_entity_manager import BrowserTopic

        em = _make_em()
        em._update_changelog_history({"added": [], "removed": [], "source": "firmware"})
        calls = [
            c for c in em.mqtt.publish.call_args_list if c.args[0] == BrowserTopic.CHANGELOG_UNREAD
        ]
        self.assertTrue(calls)
        call = calls[-1]
        payload = json.loads(call.args[1])
        self.assertEqual(payload["unread_count"], 1)
        self.assertIn("last_change", payload)
        retain = call.kwargs.get("retain", call.args[2] if len(call.args) > 2 else None)
        self.assertIs(retain, True)


class TestChangelogConsistencyProperties(unittest.TestCase):
    """Hypothesis properties for changelog data integrity."""

    def _em_with_entries(self, n_entries, age_days=0):
        import time as _time

        em = _make_em()
        em._last_prune_time = _time.time()
        ts = _time.time() - age_days * 86400
        for i in range(n_entries):
            em.change_history.appendleft(
                {
                    "timestamp": ts,
                    "iso_timestamp": "2024-01-01",
                    "added": [],
                    "removed": [],
                    "id": f"change_{i}",
                    "unread": True,
                    "source": "test",
                    "triggered_by": None,
                }
            )
        return em

    @given(st.integers(min_value=0, max_value=50))
    def test_seq_never_decreases(self, n_entries):
        """_history_seq must never decrease after operations."""
        em = self._em_with_entries(n_entries)
        seq_before = em._history_seq
        em.mark_changelog_read()
        self.assertGreaterEqual(em._history_seq, seq_before)

    @given(st.integers(min_value=0, max_value=50))
    def test_last_published_seq_leq_history_seq(self, n_entries):
        """_last_published_seq must never exceed _history_seq."""
        em = self._em_with_entries(n_entries)
        em.mark_changelog_read()
        self.assertLessEqual(em._last_published_seq, em._history_seq)

    @given(st.integers(min_value=0, max_value=50))
    def test_unread_count_consistent_after_mark_read(self, n_entries):
        """After mark_changelog_read, unread count must be 0."""
        em = self._em_with_entries(n_entries)
        em.mark_changelog_read()
        actual_unread = sum(1 for e in em.change_history if e.get("unread"))
        self.assertEqual(actual_unread, 0)

    @given(st.integers(min_value=1, max_value=50))
    def test_update_always_increments_seq(self, n_events):
        """Each _update_changelog_history call must increment _history_seq."""
        import time as _time

        em = _make_em()
        em._last_prune_time = _time.time()
        seqs = [em._history_seq]
        for i in range(n_events):
            em._update_changelog_history({"added": [i], "removed": [], "source": "test"})
            seqs.append(em._history_seq)
        # Must be strictly increasing
        self.assertEqual(seqs, sorted(set(seqs)))


class TestChangelogConsistency(unittest.TestCase):
    """Tests for changelog data integrity across publish, prune, and restart."""

    def setUp(self):
        self.em = _make_em()
        self.em._last_prune_time = time.time()  # prevent auto-prune in tests

    def _entry(self, age_days=0, seq=None):
        return {
            "timestamp": time.time() - age_days * 86400,
            "iso_timestamp": "2024-01-01",
            "added": [{"id": 6983, "title": "T", "type": "number"}],
            "removed": [],
            "id": f"change_{seq or int(time.time() * 1000)}",
            "unread": True,
        }

    def test_last_published_seq_updated_after_publish(self):
        """_last_published_seq must be set after the publish call, not before.
        This ensures a crash before publish leaves the seq guard in a state
        where the incoming retained message is not filtered on restart."""
        publish_call_count = [0]
        seq_at_publish = [None]
        original_seq = self.em._last_published_seq

        def capture_publish(topic, payload, retain=False):
            publish_call_count[0] += 1
            # Capture whether _last_published_seq has been set yet
            if publish_call_count[0] == 1:
                seq_at_publish[0] = self.em._last_published_seq

        self.em.mqtt.publish.side_effect = capture_publish

        change_event = {
            "added": [{"id": 6983, "title": "T", "type": "number"}],
            "removed": [],
            "source": "firmware",
            "triggered_by": None,
        }
        self.em._update_changelog_history(change_event)

        # At the moment of the first publish call, _last_published_seq
        # should still be the original value (updated after, not before)
        self.assertEqual(
            seq_at_publish[0], original_seq, "_last_published_seq must not be set before publish"
        )
        # After the call returns it should be updated
        self.assertGreater(self.em._last_published_seq, original_seq)

    def test_seq_guard_allows_load_when_seq_differs(self):
        """on_history_message must load the payload when incoming_seq differs
        from _last_published_seq — this is the normal restart case."""
        from nibe_entity_manager import EntityManager, _compress_payload

        self.em._last_published_seq = 5
        self.em.change_history.clear()
        EntityManager._setup_history_loading(self.em)

        payload_data = {
            "history": [self._entry()],
            "_seq": 3,  # different from _last_published_seq=5
        }
        msg = MagicMock()
        msg.payload = _compress_payload(payload_data).encode("utf-8")
        self.em._on_history_message(None, None, msg)

        self.assertEqual(len(self.em.change_history), 1)

    def test_seq_guard_skips_load_when_seq_matches(self):
        """on_history_message must skip loading when incoming_seq matches
        _last_published_seq — this prevents overwriting fresh in-memory
        history with the just-published retained copy."""
        from nibe_entity_manager import EntityManager, _compress_payload

        self.em._last_published_seq = 7
        self.em.change_history.clear()
        EntityManager._setup_history_loading(self.em)

        payload_data = {
            "history": [self._entry()],
            "_seq": 7,  # matches _last_published_seq
        }
        msg = MagicMock()
        msg.payload = _compress_payload(payload_data).encode("utf-8")
        self.em._on_history_message(None, None, msg)

        # History should remain empty — load was skipped
        self.assertEqual(len(self.em.change_history), 0)

    def test_changelog_entry_structure_is_valid_after_append(self):
        """Every entry appended by _update_changelog_history must have all
        required fields that _prune_changelog_if_due checks for."""
        change_event = {
            "added": [{"id": 6983, "title": "T", "type": "number"}],
            "removed": [],
            "source": "firmware",
            "triggered_by": None,
        }
        self.em._update_changelog_history(change_event)
        self.assertEqual(len(self.em.change_history), 1)
        entry = next(iter(self.em.change_history))
        for required_key in ("timestamp", "iso_timestamp", "added", "removed"):
            self.assertIn(required_key, entry, f"Entry missing required key: {required_key}")

    def test_unread_count_matches_unread_entries(self):
        """The unread_count published to MQTT must match the actual number
        of unread entries in change_history."""
        published_payloads = {}

        def capture(topic, payload, retain=False):
            published_payloads[topic] = payload

        self.em.mqtt.publish.side_effect = capture

        # Seed two unread entries
        self.em.change_history.appendleft({**self._entry(), "unread": True})
        self.em.change_history.appendleft({**self._entry(), "unread": True})

        change_event = {
            "added": [{"id": 6984, "title": "S", "type": "switch"}],
            "removed": [],
            "source": "firmware",
            "triggered_by": None,
        }
        self.em._update_changelog_history(change_event)

        from nibe_entity_manager import BrowserTopic

        unread_payload = published_payloads.get(str(BrowserTopic.CHANGELOG_UNREAD))
        if unread_payload:
            data = json.loads(unread_payload)
            actual_unread = sum(1 for e in self.em.change_history if e.get("unread", False))
            self.assertEqual(data["unread_count"], actual_unread)

    def test_deque_maxlen_prevents_unbounded_growth(self):
        """The deque hard cap must prevent the changelog from growing beyond
        _CHANGELOG_MAX_ENTRIES even without time-based pruning."""
        from nibe_entity_manager import _CHANGELOG_MAX_ENTRIES

        self.em._last_prune_time = time.time() + 86400  # suppress prune

        for i in range(_CHANGELOG_MAX_ENTRIES + 50):
            event = {
                "added": [{"id": i, "title": "T", "type": "sensor"}],
                "removed": [],
                "source": "firmware",
                "triggered_by": None,
            }
            self.em.change_history.appendleft(event)

        self.assertLessEqual(len(self.em.change_history), _CHANGELOG_MAX_ENTRIES)

    def test_prune_does_not_delete_below_minimum_floor(self):
        """Even with an aggressive retention setting, _CHANGELOG_MIN_ENTRIES
        must always be preserved."""
        from nibe_entity_manager import _CHANGELOG_MIN_ENTRIES

        self.em.changelog_retention_days = 1
        self.em._last_prune_time = 0.0

        # All entries are 10 days old — all expired
        for _ in range(_CHANGELOG_MIN_ENTRIES + 20):
            self.em.change_history.appendleft(self._entry(age_days=10))

        self.em._prune_changelog_if_due()
        self.assertGreaterEqual(len(self.em.change_history), _CHANGELOG_MIN_ENTRIES)


class TestMarkChangelogRead(unittest.TestCase):
    """mark_changelog_read flips all entries to unread=False and publishes
    the updated changelog with unread_count=0."""

    def _seed_history(self, em, count=3):
        from collections import deque

        em.change_history = deque(maxlen=500)
        for i in range(count):
            em.change_history.appendleft(
                {
                    "timestamp": float(i),
                    "iso_timestamp": f"2024-0{i + 1}-01T00:00:00Z",
                    "added": [i],
                    "removed": [],
                    "id": f"change_{i}",
                    "unread": True,
                    "source": "firmware",
                    "triggered_by": None,
                }
            )

    def test_all_entries_marked_read(self):
        em = _make_em()
        self._seed_history(em, 3)
        em.mark_changelog_read()
        for entry in em.change_history:
            self.assertFalse(entry["unread"])

    def test_unread_topic_published_with_zero_count(self):
        import json

        em = _make_em()
        self._seed_history(em, 2)
        em.mark_changelog_read()
        unread_calls = [c for c in em.mqtt.publish.call_args_list if "unread" in c[0][0]]
        self.assertTrue(len(unread_calls) > 0)
        payload = json.loads(unread_calls[0][0][1])
        self.assertEqual(payload["unread_count"], 0)

    def test_history_topic_published(self):
        em = _make_em()
        self._seed_history(em, 2)
        em.mark_changelog_read()
        history_calls = [c for c in em.mqtt.publish.call_args_list if "history" in c[0][0]]
        self.assertTrue(len(history_calls) > 0)

    def test_history_payload_decompresses_to_real_content(self):
        """The published history payload must actually contain the real
        history/total_entries/_seq fields the frontend card reads — not
        just 'something was published'. A mutation to
        _compress_payload(None) (publishing null instead of the real
        payload) or a renamed key would previously go uncaught."""
        import json

        from nibe_entity_manager import _decompress_payload

        em = _make_em()
        self._seed_history(em, 3)
        em.mark_changelog_read()
        history_calls = [c for c in em.mqtt.publish.call_args_list if "history" in c[0][0]]
        payload = json.loads(_decompress_payload(history_calls[0].args[1]))
        self.assertEqual(payload["total_entries"], 3)
        self.assertEqual(len(payload["history"]), 3)
        self.assertEqual(payload["_seq"], em._history_seq)

    def test_history_and_unread_topics_published_with_retain(self):
        """Both changelog topics must be retained — a caller/card that
        connects after this runs must still see the up-to-date state."""
        em = _make_em()
        self._seed_history(em, 2)
        em.mark_changelog_read()
        relevant_calls = [
            c for c in em.mqtt.publish.call_args_list if "history" in c[0][0] or "unread" in c[0][0]
        ]
        self.assertTrue(relevant_calls)
        for c in relevant_calls:
            retain = c.kwargs.get("retain", c.args[2] if len(c.args) > 2 else None)
            self.assertTrue(retain, f"Expected retain=True for {c.args[0]!r}")

    def test_unread_payload_contains_last_change_timestamp(self):
        import json

        em = _make_em()
        self._seed_history(em, 2)
        em.mark_changelog_read()
        unread_calls = [c for c in em.mqtt.publish.call_args_list if "unread" in c[0][0]]
        payload = json.loads(unread_calls[0].args[1])
        self.assertIn("last_change", payload)

    def test_seq_incremented(self):
        em = _make_em()
        self._seed_history(em, 1)
        before = em._history_seq
        em.mark_changelog_read()
        self.assertGreater(em._history_seq, before)

    def test_unread_flag_set_to_false_not_none(self):
        """entry['unread'] must become the literal False, not merely a falsy
        value. assertFalse(x) can't distinguish False from None — a mutant
        that sets entry['unread'] = None would still pass assertFalse checks
        elsewhere, so this uses assertIs against the literal False."""
        em = _make_em()
        self._seed_history(em, 3)
        em.mark_changelog_read()
        for entry in em.change_history:
            self.assertIs(entry["unread"], False)

    def test_history_seq_increments_by_exactly_one(self):
        """_history_seq += 1 must add exactly 1, not some other constant.
        Seeding a non-zero starting seq distinguishes '+= 1' from '= 1'
        (both would coincidentally pass if starting from 0)."""
        em = _make_em()
        self._seed_history(em, 1)
        em._history_seq = 41
        em.mark_changelog_read()
        self.assertEqual(em._history_seq, 42)

    def test_history_payload_contains_correct_unread_count_key(self):
        """The CHANGELOG_HISTORY payload (not just CHANGELOG_UNREAD) must
        carry an 'unread_count' key equal to 0 after marking read. A mutant
        that renames this key, or sets its value to something other than 0,
        is not exercised by the total_entries/history/_seq checks in
        test_history_payload_decompresses_to_real_content."""
        from nibe_entity_manager import _decompress_payload

        em = _make_em()
        self._seed_history(em, 3)
        em.mark_changelog_read()
        history_calls = [c for c in em.mqtt.publish.call_args_list if "history" in c[0][0]]
        payload = json.loads(_decompress_payload(history_calls[0].args[1]))
        self.assertIn("unread_count", payload)
        self.assertEqual(payload["unread_count"], 0)

    def test_history_payload_contains_last_updated_key(self):
        """A mutant that renames the 'last_updated' key in the
        CHANGELOG_HISTORY payload is not caught by any existing assertion —
        confirm the key survives under its real name."""
        from nibe_entity_manager import _decompress_payload

        em = _make_em()
        self._seed_history(em, 2)
        em.mark_changelog_read()
        history_calls = [c for c in em.mqtt.publish.call_args_list if "history" in c[0][0]]
        payload = json.loads(_decompress_payload(history_calls[0].args[1]))
        self.assertIn("last_updated", payload)

    def test_last_published_seq_updated_after_publish(self):
        """_last_published_seq must be set after the MQTT publish calls,
        not before — mirrors _update_changelog_history's own crash-safety
        test. Setting it before publish (a real bug, since fixed) meant a
        broker reconnect racing this publish (resubscribe_all() replays
        the same retained-topic callbacks on reconnect, not just on a full
        process restart) could redeliver the still-old retained message
        with a _seq that no longer matched the already-bumped
        _last_published_seq — the seq guard would then wrongly treat that
        stale message as fresh and let it clobber the just-cleared unread
        state with the old unread flags."""
        em = _make_em()
        self._seed_history(em, 3)

        publish_call_count = [0]
        seq_at_first_publish = [None]
        original_seq = em._last_published_seq

        def capture_publish(topic, payload, retain=False):
            publish_call_count[0] += 1
            if publish_call_count[0] == 1:
                seq_at_first_publish[0] = em._last_published_seq

        em.mqtt.publish.side_effect = capture_publish
        em.mark_changelog_read()

        self.assertEqual(
            seq_at_first_publish[0],
            original_seq,
            "_last_published_seq must not be set before the publish calls",
        )
        self.assertGreater(em._last_published_seq, original_seq)


class TestPruneChangelogIfDue(unittest.TestCase):
    """_prune_changelog_if_due removes entries older than retention_days,
    keeping at least 50 regardless of age. Runs at most once per hour."""

    def _entry(self, timestamp, unread=True):
        return {
            "timestamp": timestamp,
            "iso_timestamp": "2024-01-01T00:00:00Z",
            "added": [1],
            "removed": [],
            "id": "x",
            "unread": unread,
            "source": "firmware",
            "triggered_by": None,
        }

    def test_returns_false_when_not_due(self):
        em = _make_em()
        em._last_prune_time = time.time()  # just ran
        result = em._prune_changelog_if_due()
        self.assertFalse(result)

    def test_returns_true_when_due(self):
        em = _make_em()
        em._last_prune_time = 0  # never ran
        result = em._prune_changelog_if_due()
        self.assertTrue(result)

    def test_old_entries_removed(self):
        from collections import deque

        em = _make_em()
        em._last_prune_time = 0
        em.changelog_retention_days = 1  # 1 day retention
        now = time.time()
        em.change_history = deque(maxlen=500)
        # Add 55 recent and 10 old entries — total > 50 so old ones get pruned
        for _ in range(55):
            em.change_history.appendleft(self._entry(now - 100))  # recent
        for _ in range(10):
            em.change_history.appendleft(self._entry(now - 200000))  # old (>2 days)
        em._prune_changelog_if_due()
        remaining_ts = [e["timestamp"] for e in em.change_history]
        # All remaining entries should be recent (within 1 day)
        cutoff = now - 86400
        self.assertTrue(all(ts >= cutoff for ts in remaining_ts))

    def test_minimum_50_entries_kept_regardless_of_age(self):
        from collections import deque

        em = _make_em()
        em._last_prune_time = 0
        em.changelog_retention_days = 0  # expire everything
        em.change_history = deque(maxlen=500)
        # Add 60 very old entries
        for _i in range(60):
            em.change_history.appendleft(self._entry(1.0))  # epoch — very old
        em._prune_changelog_if_due()
        self.assertGreaterEqual(len(em.change_history), 50)

    def test_no_prune_when_all_entries_recent(self):
        from collections import deque

        em = _make_em()
        em._last_prune_time = 0
        em.changelog_retention_days = 90
        now = time.time()
        em.change_history = deque(maxlen=500)
        for _ in range(10):
            em.change_history.appendleft(self._entry(now - 100))
        em._prune_changelog_if_due()
        self.assertEqual(len(em.change_history), 10)

    def test_last_prune_time_updated(self):
        em = _make_em()
        em._last_prune_time = 0
        before = time.time()
        em._prune_changelog_if_due()
        self.assertGreaterEqual(em._last_prune_time, before)

    def test_due_check_boundary_is_strict_less_than(self):
        """'now - last_prune_time < _CHANGELOG_PRUNE_S' must use a strict
        '<' — at exactly the threshold, prune must run (return True), not
        be skipped. A mutant changing this to '<=' would incorrectly skip
        pruning at the exact boundary.

        The original version of this test set _last_prune_time from one
        time.time() call and let _prune_changelog_if_due() take its own
        'now' from a second, later time.time() call — the two are never
        bit-for-bit equal (real clock time always advances between the two
        calls), so 'now - last_prune_time' was always slightly ABOVE
        _CHANGELOG_PRUNE_S rather than exactly equal to it, meaning '<' and
        '<=' behaved identically and the mutant passed undetected. Mocking
        time.time() to return the exact same value for both makes the
        boundary land precisely on _CHANGELOG_PRUNE_S."""
        from unittest.mock import patch as _patch

        from nibe_entity_manager import _CHANGELOG_PRUNE_S

        em = _make_em()
        fixed_now = 1_700_000_000.0
        em._last_prune_time = fixed_now - _CHANGELOG_PRUNE_S
        with _patch("nibe_entity_manager.time.time", return_value=fixed_now):
            result = em._prune_changelog_if_due()
        self.assertTrue(result, "Prune must run at exactly the PRUNE_S boundary")

    def test_retention_cutoff_uses_86400_seconds_per_day(self):
        """cutoff_ts = now - retention_days * 86400. An entry timestamped
        just under one full day old (now - 86400.5s) must be pruned under
        a 1-day retention; a mutant multiplying by 86401 would instead
        treat it as still-recent, since now - 86401 < now - 86400.5."""
        from collections import deque

        from nibe_entity_manager import _CHANGELOG_MIN_ENTRIES

        em = _make_em()
        em._last_prune_time = 0.0
        em.changelog_retention_days = 1
        now = time.time()
        # Entries just past the 1-day boundary (old) plus enough recent
        # entries to exceed the floor, so pruning of the old ones is visible.
        recent = [self._entry(now - 10) for _ in range(_CHANGELOG_MIN_ENTRIES + 5)]
        old = [self._entry(now - 86400.5) for _ in range(5)]
        em.change_history = deque(recent + old, maxlen=500)
        em._prune_changelog_if_due()
        self.assertEqual(
            len(em.change_history),
            len(recent),
            "Entries just past the 86400s/day boundary must be pruned",
        )

    def test_recent_old_boundary_entry_counted_exactly_once(self):
        """An entry with timestamp exactly equal to cutoff_ts must land in
        'recent' (>= cutoff_ts) and NOT also in 'old' (< cutoff_ts) — the
        two filters must partition entries, not overlap. A mutant changing
        '>=' to '>' would drop the boundary entry from recent (losing it if
        the floor is small); one changing '<' to '<=' would put it in both
        lists, so it would appear twice in the final kept list.

        The original version computed cutoff_ts from its own time.time()
        call, then let _prune_changelog_if_due() recompute cutoff_ts from a
        second, later time.time() call — the two never matched exactly (real
        clock time advances between calls), so the "boundary" entry actually
        landed clearly inside 'old', never testing the boundary at all.
        Mocking time.time() makes both cutoff_ts computations use the exact
        same value."""
        from collections import deque
        from unittest.mock import patch as _patch

        from nibe_entity_manager import _CHANGELOG_MIN_ENTRIES

        em = _make_em()
        em._last_prune_time = 0.0
        em.changelog_retention_days = 1
        fixed_now = 1_700_000_000.0
        cutoff_ts = fixed_now - 1 * 86400
        boundary_entry = self._entry(0)
        boundary_entry["timestamp"] = cutoff_ts
        boundary_entry["id"] = "boundary"
        # Enough other very-old entries that the floor doesn't mask pruning,
        # and enough total that "kept" isn't trivially "everything".
        filler = [self._entry(fixed_now - 999 * 86400 - 100) for _ in range(_CHANGELOG_MIN_ENTRIES)]
        em.change_history = deque([boundary_entry] + filler, maxlen=500)
        with _patch("nibe_entity_manager.time.time", return_value=fixed_now):
            em._prune_changelog_if_due()
        boundary_count = sum(1 for e in em.change_history if e.get("id") == "boundary")
        self.assertEqual(
            boundary_count,
            1,
            "Boundary entry must be kept exactly once (via 'recent'), not dropped or duplicated",
        )

    def test_no_reassignment_or_log_when_nothing_pruned(self):
        """When pruned == len(history) - len(kept) is 0, the deque must not
        be reassigned and the debug log must not fire. A mutant computing
        'pruned' via '+' instead of '-' (or relaxing 'pruned > 0' to '>= 0')
        would make this branch fire even when nothing was actually removed;
        since the reassigned content would be identical, only the log call
        distinguishes correct from mutant behaviour here."""
        from unittest.mock import patch as _patch

        em = _make_em()
        em._last_prune_time = 0.0
        em.changelog_retention_days = 90  # nothing expires
        for _ in range(5):
            em.change_history.appendleft(self._entry(0))
        with _patch("nibe_entity_manager.log_history") as mock_log:
            em._prune_changelog_if_due()
            mock_log.debug.assert_not_called()

    def test_maxlen_preserved_after_actual_pruning(self):
        """When pruning actually removes entries (pruned > 0), the
        replacement deque must keep the original maxlen. The existing
        'test_maxlen_preserved' test only has 1 entry, so pruned == 0 and
        the reassignment branch (where the maxlen mutation lives) never
        executes — this test forces real pruning to occur."""
        from collections import deque

        from nibe_entity_manager import _CHANGELOG_MIN_ENTRIES

        em = _make_em()
        em._last_prune_time = 0.0
        em.changelog_retention_days = 1
        now = time.time()
        ml = 500
        recent = [self._entry(now - 10) for _ in range(_CHANGELOG_MIN_ENTRIES + 5)]
        old = [self._entry(now - 999 * 86400) for _ in range(10)]
        em.change_history = deque(recent + old, maxlen=ml)
        em._prune_changelog_if_due()
        self.assertLess(
            len(em.change_history),
            len(recent) + len(old),
            "Sanity check: pruning must actually have removed entries",
        )
        self.assertEqual(em.change_history.maxlen, ml)


class TestSetupHistoryLoadingCallbacks(unittest.TestCase):
    """_setup_history_loading: empty payload, exception path."""

    def _make_message(self, payload):
        msg = MagicMock()
        msg.payload = payload
        return msg

    def test_empty_payload_returns_without_loading(self):
        em = _make_em()
        from nibe_entity_manager import EntityManager

        EntityManager._setup_history_loading(em)
        em._on_history_message(None, None, self._make_message(b""))
        # change_history should be untouched
        self.assertEqual(len(em.change_history), 0)

    def test_bad_payload_resets_history(self):
        em = _make_em()
        from nibe_entity_manager import EntityManager

        EntityManager._setup_history_loading(em)
        em._on_history_message(None, None, self._make_message(b"not valid json or gzip"))
        # Should reset to empty deque rather than crash
        self.assertEqual(len(em.change_history), 0)


class TestSetupHistoryLoadingUnreadCallback(unittest.TestCase):
    """on_unread_message: empty payload, valid payload, exception."""

    def _make_message(self, payload):
        msg = MagicMock()
        msg.payload = payload
        return msg

    def test_empty_payload_does_not_crash(self):
        em = _make_em()
        from nibe_entity_manager import EntityManager

        EntityManager._setup_history_loading(em)
        em._on_unread_message(None, None, self._make_message(b""))

    def test_valid_unread_marks_entries(self):
        """Regression: appendleft(1) then appendleft(2) makes change_history
        = [2, 1] (2 is newest, at index 0). unread_count=1 must mark the
        NEWEST entry (id=2, index 0) unread — not the oldest (id=1, index -1),
        which is what the pre-fix list(...)[-n:] slice incorrectly selected."""
        em = _make_em()
        from nibe_entity_manager import EntityManager

        EntityManager._setup_history_loading(em)
        em.change_history.appendleft({"unread": False, "id": 1})
        em.change_history.appendleft({"unread": False, "id": 2})
        payload = json.dumps({"unread_count": 1}).encode()
        em._on_unread_message(None, None, self._make_message(payload))
        entries = list(em.change_history)
        self.assertTrue(entries[0]["unread"], "newest entry (id=2) must be marked unread")
        self.assertFalse(entries[-1]["unread"], "oldest entry (id=1) must stay read")

    def test_bad_unread_payload_does_not_crash(self):
        em = _make_em()
        from nibe_entity_manager import EntityManager

        EntityManager._setup_history_loading(em)
        em._on_unread_message(None, None, self._make_message(b"NOT JSON"))


class TestHistoryLoadingEntryValidation(unittest.TestCase):
    """_on_history_message: skip non-dict and malformed entries."""

    def _make_message(self, payload):
        msg = MagicMock()
        msg.payload = payload
        return msg

    def _compress(self, data):
        from nibe_entity_manager import _compress_payload

        return _compress_payload(data).encode("utf-8")

    def _valid_entry(self):
        return {
            "timestamp": 1700000000.0,
            "iso_timestamp": "2024-01-01",
            "added": [{"id": 100, "title": "T", "type": "sensor"}],
            "removed": [],
            "id": "change_1",
            "unread": False,
            "source": "firmware",
        }

    def test_non_dict_entry_skipped_valid_entry_kept(self):
        """Non-dict history entries must be skipped; valid ones retained."""
        em = _make_em()
        from nibe_entity_manager import EntityManager

        EntityManager._setup_history_loading(em)
        payload_data = {
            "history": ["not a dict", 42, self._valid_entry()],
        }
        em._on_history_message(None, None, self._make_message(self._compress(payload_data)))
        self.assertEqual(len(em.change_history), 1)
        self.assertEqual(em.change_history[0]["id"], "change_1")

    def test_entry_with_non_list_added_skipped(self):
        """Entry where 'added' is not a list must be skipped."""
        em = _make_em()
        from nibe_entity_manager import EntityManager

        EntityManager._setup_history_loading(em)
        bad = dict(self._valid_entry())
        bad["added"] = "should_be_a_list"
        payload_data = {"history": [bad, self._valid_entry()]}
        em._on_history_message(None, None, self._make_message(self._compress(payload_data)))
        self.assertEqual(len(em.change_history), 1)

    def test_entry_with_non_list_removed_skipped(self):
        """Entry where 'removed' is not a list must be skipped."""
        em = _make_em()
        from nibe_entity_manager import EntityManager

        EntityManager._setup_history_loading(em)
        bad = dict(self._valid_entry())
        bad["removed"] = {"wrong": "type"}
        payload_data = {"history": [bad, self._valid_entry()]}
        em._on_history_message(None, None, self._make_message(self._compress(payload_data)))
        self.assertEqual(len(em.change_history), 1)


class TestOnHistoryMessageMissingHistoryKey(unittest.TestCase):
    """on_history_message: 2620→exit — payload has no 'history' key
    (or it is not a list).

    The handler must silently return without touching change_history.
    """

    def _make_message(self, payload_str):
        """Wrap a string payload (the bridge uses 'gzip1:<base64>' format)."""
        msg = MagicMock()
        msg.payload = payload_str.encode() if isinstance(payload_str, str) else payload_str
        return msg

    def _pack(self, data):
        """Produce a valid bridge-format payload using _compress_payload."""
        from nibe_entity_manager import _compress_payload

        return _compress_payload(data)

    def test_missing_history_key_does_not_touch_change_history(self):
        from nibe_entity_manager import EntityManager

        em = _make_em()
        EntityManager._setup_history_loading(em)
        em.change_history.appendleft({"id": 1, "unread": False})
        # Payload with 'incoming_seq' but no 'history' key
        em._on_history_message(None, None, self._make_message(self._pack({"incoming_seq": 99})))
        # change_history must be untouched — 'history' key absent
        self.assertEqual(len(em.change_history), 1)
        self.assertEqual(next(iter(em.change_history))["id"], 1)

    def test_history_not_a_list_does_not_touch_change_history(self):
        from nibe_entity_manager import EntityManager

        em = _make_em()
        EntityManager._setup_history_loading(em)
        em.change_history.appendleft({"id": 2, "unread": False})
        # 'history' present but wrong type (string instead of list)
        em._on_history_message(
            None, None, self._make_message(self._pack({"incoming_seq": 5, "history": "not a list"}))
        )
        self.assertEqual(len(em.change_history), 1)


class TestOnUnreadMessageZeroCount(unittest.TestCase):
    """on_unread_message: 2661→exit — unread_count=0 with non-empty history.

    When the count is 0, the 'if unread_count > 0 and change_history' guard
    is False so no entries are marked as unread.  All entries must remain
    with unread=False.
    """

    def _make_message(self, payload_bytes):
        msg = MagicMock()
        msg.payload = payload_bytes
        return msg

    def test_zero_unread_count_leaves_entries_unread_false(self):
        import json as _json

        from nibe_entity_manager import EntityManager

        em = _make_em()
        EntityManager._setup_history_loading(em)
        em.change_history.appendleft({"unread": False, "id": 1})
        em.change_history.appendleft({"unread": False, "id": 2})
        payload = _json.dumps({"unread_count": 0}).encode()
        em._on_unread_message(None, None, self._make_message(payload))
        for entry in em.change_history:
            self.assertFalse(entry["unread"], "unread_count=0 must not mark any entry as unread")

    def test_negative_unread_count_leaves_entries_unread_false(self):
        """A negative unread_count (malformed/adversarial retained payload)
        must not mark any entry as unread either. The guard is written as
        `if unread_count > 0 and change_history:` — if it were `or`
        instead of `and`, a negative count would still enter the branch
        (since change_history is truthy), and safe_count = min(negative, len)
        stays negative, so list(...)[:safe_count] silently excludes only the
        last |n| entries via Python's negative-slice semantics — marking
        nearly the WHOLE history unread instead of none."""
        import json as _json

        from nibe_entity_manager import EntityManager

        em = _make_em()
        EntityManager._setup_history_loading(em)
        for i in range(5):
            em.change_history.appendleft({"unread": False, "id": i})
        payload = _json.dumps({"unread_count": -3}).encode()
        em._on_unread_message(None, None, self._make_message(payload))
        for entry in em.change_history:
            self.assertFalse(
                entry["unread"], "negative unread_count must not mark any entry as unread"
            )


class TestUnreadCountOverfillGuard(unittest.TestCase):
    """on_unread_message: unread_count larger than len(change_history)
    must not over-mark entries.

    E5 regression: before the fix, list[-n:] with n > len returned the full
    list, marking every entry as unread regardless of the claimed count.
    The broker's retained count can exceed the in-memory history length after
    a prune; the fix caps safe_count = min(unread_count, len(change_history)).
    """

    def _make_message(self, payload_bytes):
        msg = MagicMock()
        msg.payload = payload_bytes
        return msg

    def test_oversized_unread_count_marks_at_most_all_entries(self):
        """unread_count=10 with only 2 history entries marks both, not a crash."""
        from nibe_entity_manager import EntityManager

        em = _make_em()
        EntityManager._setup_history_loading(em)
        em.change_history.appendleft({"unread": False, "id": 1})
        em.change_history.appendleft({"unread": False, "id": 2})
        self.assertEqual(len(em.change_history), 2)

        payload = json.dumps({"unread_count": 10}).encode()
        em._on_unread_message(None, None, self._make_message(payload))

        unread = [e for e in em.change_history if e.get("unread")]
        self.assertEqual(
            len(unread), 2, "unread_count > len(history) should mark all entries, not crash"
        )

    def test_unread_count_less_than_history_marks_newest_only(self):
        """unread_count=2 with 5 history entries marks only the 2 newest.

        Regression: entries are appendleft()-ed everywhere in this module
        (_update_changelog_history), so index 0 is the NEWEST entry and the
        deque's tail is the OLDEST. The fix must select the newest N via
        list(...)[:n] (the head) — the previous list(...)[-n:] (the tail)
        marked the OLDEST entries unread instead, which this test's
        predecessor (asserting only a count, not which entries) never
        caught."""
        from nibe_entity_manager import EntityManager

        em = _make_em()
        EntityManager._setup_history_loading(em)
        for i in range(5):
            em.change_history.appendleft({"unread": False, "id": i})
        # appendleft(0..4) in order -> change_history is [4,3,2,1,0] (newest first)

        payload = json.dumps({"unread_count": 2}).encode()
        em._on_unread_message(None, None, self._make_message(payload))

        unread_ids = {e["id"] for e in em.change_history if e.get("unread")}
        self.assertEqual(
            unread_ids,
            {4, 3},
            "unread_count=2 must mark the 2 NEWEST entries (ids 4, 3), not the 2 oldest (ids 0, 1)",
        )

    def test_unread_count_zero_marks_nothing(self):
        """unread_count=0 must leave all entries as unread=False."""
        from nibe_entity_manager import EntityManager

        em = _make_em()
        EntityManager._setup_history_loading(em)
        for i in range(3):
            em.change_history.appendleft({"unread": False, "id": i})

        payload = json.dumps({"unread_count": 0}).encode()
        em._on_unread_message(None, None, self._make_message(payload))

        for entry in em.change_history:
            self.assertFalse(entry["unread"], "unread_count=0 must not mark any entry as unread")


class TestSetupHistoryLoadingCleanedEntryFields(unittest.TestCase):
    """on_history_message builds a 'cleaned' dict per incoming history entry
    with 8 fields, each pulled via entry.get(key, default). These tests
    close survivors in that field-by-field construction using two
    independent scenarios per field:
      - Scenario A: every source key present with a distinct sentinel value
        -> catches any mutation to the *lookup key* or the *output dict
          key* (both cause the sentinel to be lost).
      - Scenario B: source entry deliberately empty -> catches any mutation
        to the *default value* (only observable when the key is absent).
    Neither scenario alone would catch both mutation families; together
    they do, without any tautological readback from the mock under test.
    """

    def _make_message(self, payload_str):
        msg = MagicMock()
        msg.payload = payload_str.encode() if isinstance(payload_str, str) else payload_str
        return msg

    def _pack(self, data):
        from nibe_entity_manager import _compress_payload

        return _compress_payload(data)

    def _load_and_get_first(self, em, history_entry):
        from nibe_entity_manager import EntityManager

        EntityManager._setup_history_loading(em)
        em._on_history_message(
            None, None, self._make_message(self._pack({"history": [history_entry]}))
        )
        self.assertEqual(
            len(em.change_history),
            1,
            "Entry must have been accepted (added/removed must resolve to lists)",
        )
        return em.change_history[0]

    def test_all_fields_preserved_when_present(self):
        """Scenario A: every field present with a distinct sentinel value.
        Any lookup-key or output-key mutation replaces the sentinel with
        something else (often the default, sometimes None/KeyError)."""
        em = _make_em()
        source_entry = {
            "timestamp": 12345.5,
            "iso_timestamp": "ISO_SENTINEL",
            "added": [111],
            "removed": [222],
            "id": "ID_SENTINEL",
            "unread": True,
            "source": "SRC_SENTINEL",
            "triggered_by": "TRIG_SENTINEL",
        }
        cleaned = self._load_and_get_first(em, source_entry)
        self.assertEqual(cleaned["timestamp"], 12345.5)
        self.assertEqual(cleaned["iso_timestamp"], "ISO_SENTINEL")
        self.assertEqual(cleaned["added"], [111])
        self.assertEqual(cleaned["removed"], [222])
        self.assertEqual(cleaned["id"], "ID_SENTINEL")
        self.assertIs(cleaned["unread"], True)
        self.assertEqual(cleaned["source"], "SRC_SENTINEL")
        self.assertEqual(cleaned["triggered_by"], "TRIG_SENTINEL")

    def test_defaults_applied_when_fields_absent(self):
        """Scenario B: source entry has none of the optional fields. Each
        cleaned field must fall back to its real default, not None and not
        some other placeholder. 'added'/'removed' default to [] (a list),
        which is required for the entry to even be kept — so this also
        implicitly confirms those defaults are correct."""
        em = _make_em()
        cleaned = self._load_and_get_first(em, {})
        self.assertIsInstance(cleaned["timestamp"], float)
        self.assertGreater(cleaned["timestamp"], 0)
        self.assertIsInstance(cleaned["iso_timestamp"], str)
        self.assertGreater(len(cleaned["iso_timestamp"]), 0)
        self.assertEqual(cleaned["added"], [])
        self.assertEqual(cleaned["removed"], [])
        self.assertTrue(cleaned["id"].startswith("change_"))
        self.assertIs(cleaned["unread"], False)
        self.assertEqual(cleaned["source"], "firmware")
        self.assertIsNone(cleaned["triggered_by"])

    def test_default_id_computed_from_time_times_1000(self):
        """When 'id' is absent, it must be f"change_{int(time.time()*1000)}"
        — checked against an independently fixed, mocked time value (not
        read back from the entry), to catch '*1001'/'/1000' scaling
        mutants that a mere 'startswith(change_)' check would miss."""
        from unittest.mock import patch as _patch

        em = _make_em()
        fixed_time = 1_650_000_777.25
        with _patch("nibe_entity_manager.time.time", return_value=fixed_time):
            cleaned = self._load_and_get_first(em, {"added": [], "removed": []})
        expected_id = f"change_{int(fixed_time * 1000)}"
        self.assertEqual(cleaned["id"], expected_id)


class TestSetupHistoryLoadingSeqGuard(unittest.TestCase):
    """The incoming_seq guard: 'if incoming_seq != -1 and incoming_seq ==
    self._last_published_seq: return'. -1 is the sentinel meaning 'unknown
    seq, always load'. These tests exercise the default (-1) applied when
    '_seq' is absent, and the '-1' literal in the comparison itself."""

    def _make_message(self, payload_str):
        msg = MagicMock()
        msg.payload = payload_str.encode() if isinstance(payload_str, str) else payload_str
        return msg

    def _pack(self, data):
        from nibe_entity_manager import _compress_payload

        return _compress_payload(data)

    def test_missing_seq_key_always_loads_regardless_of_last_published_seq(self):
        """When '_seq' is absent from the payload, incoming_seq must default
        to -1, and per the guard's own special-casing of -1, loading must
        proceed no matter what _last_published_seq happens to be — even if
        it coincidentally equals a wrong default a mutant might introduce
        (None, 1, or -2)."""
        from nibe_entity_manager import EntityManager

        for wrong_default_lookalike in (None, 1, -2):
            with self.subTest(last_published_seq=wrong_default_lookalike):
                em = _make_em()
                em._last_published_seq = wrong_default_lookalike
                EntityManager._setup_history_loading(em)
                em._on_history_message(
                    None,
                    None,
                    self._make_message(self._pack({"history": [{"added": [], "removed": []}]})),
                )
                self.assertEqual(
                    len(em.change_history),
                    1,
                    "Missing '_seq' must always result in loading (default -1 short-circuits the guard)",
                )

    def test_seq_minus_one_always_loads_even_if_last_published_seq_is_minus_one(self):
        """The '-1' sentinel is special: even if incoming_seq == -1 happens
        to equal _last_published_seq == -1, the first clause of the AND
        ('incoming_seq != -1') is False, so the guard must NOT skip loading.
        A mutant changing '!= -1' to '!= 1' or '!= -2' would incorrectly
        skip this exact case, since -1 != 1 and -1 != -2 are both True."""
        from nibe_entity_manager import EntityManager

        em = _make_em()
        em._last_published_seq = -1
        EntityManager._setup_history_loading(em)
        em._on_history_message(
            None,
            None,
            self._make_message(self._pack({"_seq": -1, "history": [{"added": [], "removed": []}]})),
        )
        self.assertEqual(
            len(em.change_history),
            1,
            "incoming_seq == -1 must always load, regardless of _last_published_seq",
        )


class TestSetupHistoryLoadingCleanHistoryMaxlen(unittest.TestCase):
    def test_maxlen_preserved_on_successful_load(self):
        """clean_history = deque(maxlen=self.change_history.maxlen) — a
        mutant setting maxlen=None would silently make the changelog
        unbounded after every restart."""
        from nibe_entity_manager import EntityManager

        em = _make_em()
        original_maxlen = em.change_history.maxlen
        self.assertIsNotNone(original_maxlen)
        EntityManager._setup_history_loading(em)
        msg = MagicMock()
        from nibe_entity_manager import _compress_payload

        msg.payload = _compress_payload({"history": [{"added": [], "removed": []}]}).encode("utf-8")
        em._on_history_message(None, None, msg)
        self.assertEqual(em.change_history.maxlen, original_maxlen)


class TestSetupHistoryLoadingUnreadCountLogging(unittest.TestCase):
    """After a successful load, the handler computes
    unread = sum(1 for e in self.change_history if e.get('unread', False))
    and logs it via log_history.info(...). Since 'unread' is always present
    on freshly-cleaned entries (set via .get(..., False/True) during
    cleaning), a wrong *default* here is unobservable — but a wrong *key*
    (looking up 'XXunreadXX'/'UNREAD'/None instead of 'unread') or a wrong
    per-item weight (sum(2 for ...) instead of sum(1 for ...)) changes the
    logged count, and the boundary '> 0' vs '>= 0' / '> 1' changes whether
    the second log line fires at all.
    """

    def _make_message(self, payload_str):
        msg = MagicMock()
        msg.payload = payload_str.encode() if isinstance(payload_str, str) else payload_str
        return msg

    def _pack(self, data):
        from nibe_entity_manager import _compress_payload

        return _compress_payload(data)

    def test_loaded_count_log_uses_real_format_and_length(self):
        """The 'Loaded %d historical changes from MQTT' log call must carry
        the exact literal format string and the real entry count — not a
        different arg shape (single positional int, missing second arg) or
        altered text."""
        from unittest.mock import patch as _patch

        from nibe_entity_manager import EntityManager

        em = _make_em()
        EntityManager._setup_history_loading(em)
        entries = [{"added": [i], "removed": []} for i in range(3)]
        with _patch("nibe_entity_manager.log_history") as mock_log:
            em._on_history_message(None, None, self._make_message(self._pack({"history": entries})))
            loaded_calls = [
                c
                for c in mock_log.info.call_args_list
                if c.args and isinstance(c.args[0], str) and c.args[0].startswith("Loaded")
            ]
        self.assertEqual(len(loaded_calls), 1)
        self.assertEqual(loaded_calls[0].args, ("Loaded %d historical changes from MQTT", 3))

    def test_unread_count_in_log_matches_actual_unread_entries(self):
        """One unread + one read entry loaded -> the '%d unread changes'
        log call must report exactly 1. A mutant looking up the wrong key
        ('XXunreadXX'/'UNREAD'/None) would always see 0 unread (since real
        entries only ever carry the real 'unread' key), suppressing this
        log line entirely; a mutant using sum(2 for ...) would report 2."""
        from unittest.mock import patch as _patch

        from nibe_entity_manager import EntityManager

        em = _make_em()
        EntityManager._setup_history_loading(em)
        entries = [
            {"added": [1], "removed": [], "unread": True},
            {"added": [2], "removed": [], "unread": False},
        ]
        with _patch("nibe_entity_manager.log_history") as mock_log:
            em._on_history_message(None, None, self._make_message(self._pack({"history": entries})))
            unread_calls = [
                c
                for c in mock_log.info.call_args_list
                if c.args and isinstance(c.args[0], str) and "unread changes" in c.args[0]
            ]
        self.assertEqual(
            len(unread_calls),
            1,
            "Exactly one unread entry must trigger exactly one '%d unread changes' log call",
        )
        self.assertEqual(unread_calls[0].args, ("%d unread changes", 1))

    def test_no_unread_log_line_when_zero_unread(self):
        """With zero unread entries, the '%d unread changes' log line must
        not fire at all — a mutant relaxing 'unread > 0' to 'unread >= 0'
        would log it unconditionally."""
        from unittest.mock import patch as _patch

        from nibe_entity_manager import EntityManager

        em = _make_em()
        EntityManager._setup_history_loading(em)
        entries = [{"added": [1], "removed": [], "unread": False}]
        with _patch("nibe_entity_manager.log_history") as mock_log:
            em._on_history_message(None, None, self._make_message(self._pack({"history": entries})))
            unread_calls = [
                c
                for c in mock_log.info.call_args_list
                if c.args and isinstance(c.args[0], str) and "unread changes" in c.args[0]
            ]
        self.assertEqual(len(unread_calls), 0)


class TestSetupHistoryLoadingExceptionPaths(unittest.TestCase):
    """Both on_history_message and on_unread_message wrap their body in a
    try/except Exception that logs a warning. These tests confirm the
    warning is logged with the real literal message and the real exception
    object (not None / a truncated arg list / altered text), and that the
    history-message exception handler resets change_history using the
    original maxlen (not None)."""

    def _make_message(self, payload_bytes):
        msg = MagicMock()
        msg.payload = payload_bytes
        return msg

    def test_history_message_exception_logs_real_warning_with_exception_object(self):
        from unittest.mock import patch as _patch

        from nibe_entity_manager import EntityManager

        em = _make_em()
        EntityManager._setup_history_loading(em)
        with _patch("nibe_entity_manager.log_history") as mock_log:
            em._on_history_message(None, None, self._make_message(b"not valid json or gzip"))
            self.assertEqual(mock_log.warning.call_count, 1)
            call = mock_log.warning.call_args
        self.assertEqual(
            call.args[0],
            "Could not load changelog history from MQTT retained message "
            "(the message may be from an older bridge version): %s",
        )
        self.assertIsInstance(call.args[1], Exception)

    def test_history_message_exception_resets_maxlen_not_none(self):
        """The exception-path reset 'self.change_history = deque(maxlen=
        self.change_history.maxlen)' must preserve the real maxlen."""
        from nibe_entity_manager import EntityManager

        em = _make_em()
        original_maxlen = em.change_history.maxlen
        self.assertIsNotNone(original_maxlen)
        EntityManager._setup_history_loading(em)
        em._on_history_message(None, None, self._make_message(b"garbage"))
        self.assertEqual(em.change_history.maxlen, original_maxlen)

    def test_unread_message_exception_logs_real_warning_with_exception_object(self):
        from unittest.mock import patch as _patch

        from nibe_entity_manager import EntityManager

        em = _make_em()
        EntityManager._setup_history_loading(em)
        with _patch("nibe_entity_manager.log_history") as mock_log:
            em._on_unread_message(None, None, self._make_message(b"not json"))
            self.assertEqual(mock_log.warning.call_count, 1)
            call = mock_log.warning.call_args
        self.assertEqual(call.args[0], "Could not restore changelog unread state from MQTT: %s")
        self.assertIsInstance(call.args[1], Exception)


class TestSetupHistoryLoadingUnreadDefaultAndResetKey(unittest.TestCase):
    """on_unread_message: the default for 'unread_count' when the key is
    absent, and the reset loop that sets entry['unread'] = False for every
    entry before re-marking a tail as unread."""

    def _make_message(self, payload_bytes):
        msg = MagicMock()
        msg.payload = payload_bytes
        return msg

    def test_missing_unread_count_key_defaults_to_zero_no_warning(self):
        """'unread_count' absent from payload must default to 0 (no entries
        marked, no exception/warning) — not None (which would raise
        TypeError on the '> 0' comparison, caught by the outer except and
        logged as a warning) and not some non-zero value (which would mark
        entries that shouldn't be marked)."""
        from unittest.mock import patch as _patch

        from nibe_entity_manager import EntityManager

        em = _make_em()
        EntityManager._setup_history_loading(em)
        for i in range(3):
            em.change_history.appendleft({"unread": False, "id": i})
        with _patch("nibe_entity_manager.log_history") as mock_log:
            em._on_unread_message(None, None, self._make_message(json.dumps({}).encode()))
            mock_log.warning.assert_not_called()
        for entry in em.change_history:
            self.assertIs(entry["unread"], False)

    def test_reset_loop_sets_real_false_via_real_key(self):
        """Every entry must have its ACTUAL 'unread' key (not a renamed
        'XXunreadXX'/'UNREAD') set to the literal False during the reset
        pass. Seeding entries as unread=True first, then sending
        unread_count=0, makes a renamed-key mutant observable: the real
        'unread' key would be left untouched at True instead of being
        reset."""
        from nibe_entity_manager import EntityManager

        em = _make_em()
        EntityManager._setup_history_loading(em)
        em.change_history.appendleft({"unread": True, "id": 1})
        em.change_history.appendleft({"unread": True, "id": 2})
        em._on_unread_message(
            None, None, self._make_message(json.dumps({"unread_count": 0}).encode())
        )
        for entry in em.change_history:
            self.assertIs(entry["unread"], False)


class TestSetupHistoryLoadingMqttWiring(unittest.TestCase):
    """_setup_history_loading must subscribe to both changelog topics and
    register the matching callback for each — using the real topics and the
    real (stored) callback functions, not None or the wrong pairing."""

    def test_subscribe_and_callback_wiring_uses_real_topics_and_callbacks(self):
        from nibe_entity_manager import BrowserTopic, EntityManager

        em = _make_em()
        EntityManager._setup_history_loading(em)

        subscribed_topics = [c.args[0] for c in em.mqtt.subscribe.call_args_list]
        self.assertIn(BrowserTopic.CHANGELOG_HISTORY, subscribed_topics)
        self.assertIn(BrowserTopic.CHANGELOG_UNREAD, subscribed_topics)

        callback_pairs = {c.args[0]: c.args[1] for c in em.mqtt.message_callback_add.call_args_list}
        self.assertEqual(callback_pairs.get(BrowserTopic.CHANGELOG_HISTORY), em._on_history_message)
        self.assertEqual(callback_pairs.get(BrowserTopic.CHANGELOG_UNREAD), em._on_unread_message)


class TestChangelogHistorySurvivesSimulatedRestart(unittest.TestCase):
    """_update_changelog_history() (writer) and _setup_history_loading()'s
    on_history_message (reader) are each thoroughly tested above — but
    always against hand-built payloads matching what the writer *would*
    produce, never a payload the real writer actually produced. This
    chains the two real functions together on a fresh EntityManager
    instance (a genuine restart simulation, not two calls on the same
    instance), with nothing hand-substituted in between."""

    def test_real_history_entry_survives_a_fresh_instance_restart(self):
        writer = _make_em()
        change_event = {
            "added": [{"id": 12345, "title": "New Point", "type": "sensor"}],
            "removed": [],
            "source": "firmware",
            "triggered_by": None,
        }
        writer._update_changelog_history(change_event)
        # Find the CHANGELOG_HISTORY publish call by its actual decompressed
        # content rather than matching on the topic name (BrowserTopic is an
        # enum, not a plain string, so string-matching it here would be
        # brittle) — the same technique both new tests in this class use.
        retained_payload = None
        for c in writer.mqtt.publish.call_args_list:
            try:
                from nibe_entity_manager import _decompress_payload

                decoded = json.loads(_decompress_payload(c.args[1]))
                if "history" in decoded:
                    retained_payload = c.args[1]
                    break
            except Exception:  # noqa: BLE001, S112 — scanning every publish()
                # call's payload, most of which aren't history payloads at
                # all (wrong topic, plain string, uncompressed JSON without
                # a 'history' key); any decode failure here just means "not
                # the one we want," not a real error to surface.
                continue
        self.assertIsNotNone(retained_payload, "writer never published a decodable history payload")

        reader = _make_em()
        from nibe_entity_manager import EntityManager

        EntityManager._setup_history_loading(reader)
        msg = MagicMock()
        msg.payload = retained_payload
        reader._on_history_message(None, None, msg)

        self.assertEqual(len(reader.change_history), 1)
        restored_entry = reader.change_history[0]
        self.assertEqual(
            restored_entry["added"], [{"id": 12345, "title": "New Point", "type": "sensor"}]
        )
        self.assertEqual(restored_entry["source"], "firmware")
        self.assertTrue(restored_entry["unread"])

    def test_mark_read_by_writer_is_reflected_after_fresh_instance_restart(self):
        """The unread=False state set by a real mark_changelog_read() call
        must survive into a fresh instance's real on_history_message —
        not just the raw entries, but their unread flags too."""
        writer = _make_em()
        writer._update_changelog_history(
            {
                "added": [{"id": 1, "title": "P1", "type": "sensor"}],
                "removed": [],
                "source": "firmware",
                "triggered_by": None,
            }
        )
        writer.mqtt.publish.reset_mock()
        writer.mark_changelog_read()

        from nibe_entity_manager import _decompress_payload

        retained_payload = None
        for c in writer.mqtt.publish.call_args_list:
            try:
                decoded = json.loads(_decompress_payload(c.args[1]))
                if "history" in decoded:
                    retained_payload = c.args[1]
                    break
            except Exception:  # noqa: BLE001, S112 — same reasoning as the
                # identical scan in test_real_history_entry_survives_a_fresh_
                # instance_restart above.
                continue
        self.assertIsNotNone(retained_payload)

        reader = _make_em()
        from nibe_entity_manager import EntityManager

        EntityManager._setup_history_loading(reader)
        msg = MagicMock()
        msg.payload = retained_payload
        reader._on_history_message(None, None, msg)

        self.assertEqual(len(reader.change_history), 1)
        self.assertFalse(reader.change_history[0]["unread"])
