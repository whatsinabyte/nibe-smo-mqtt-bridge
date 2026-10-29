"""
Tests for nibe_attribution.py — the pure decisions behind linking points that
appear in a bulk poll to the switch or select that made them appear.

EntityManager's own tests (test_entity_manager_dynamic.py and friends) cover
the same behaviour through the methods that gather these inputs; these test
the decisions directly, as properties over arbitrary inputs.
"""

import unittest

from hypothesis import example, given
from hypothesis import strategies as st
from nibe_attribution import (
    ControllerSide,
    changed_candidates,
    controller_side_decision,
    late_arrival_decision,
    late_tail_until,
    learning_outcome,
    poll_verdict,
    recordable_value,
    version_changed,
)

ids = st.integers(min_value=1, max_value=40)
id_sets = st.frozensets(ids, max_size=12)
values = st.one_of(st.none(), st.integers(min_value=0, max_value=4))
times = st.floats(min_value=0, max_value=1e6, allow_nan=False)


def _verdict(**overrides):
    args = {
        "discovery_complete": True,
        "published": {1, 2},
        "now": 100.0,
        "blocked_until": 0.0,
        "other_write_pending": False,
        "served": {1, 2},
        "known_dynamic": set(),
        "versions_changed": False,
    }
    args.update(overrides)
    return poll_verdict(**args)


class TestPollVerdict(unittest.TestCase):
    def test_steady_poll_is_attributable(self):
        self.assertEqual(_verdict(), "ok")

    def test_new_points_alone_are_attributable(self):
        self.assertEqual(_verdict(served={1, 2, 3}), "ok")

    def test_before_initial_discovery_skips(self):
        self.assertEqual(_verdict(discovery_complete=False), "skip")

    def test_nothing_published_yet_skips(self):
        self.assertEqual(_verdict(published=set()), "skip")

    def test_blocked_skips_until_the_block_ends(self):
        self.assertEqual(_verdict(blocked_until=100.5), "skip")
        self.assertEqual(_verdict(blocked_until=100.0), "ok")

    def test_another_write_pending_skips(self):
        self.assertEqual(_verdict(other_write_pending=True), "skip")

    def test_missing_static_point_blocks(self):
        self.assertEqual(_verdict(served={1}), "block")

    def test_missing_known_dynamic_point_does_not_block(self):
        self.assertEqual(_verdict(served={1}, known_dynamic={2}), "ok")

    def test_version_change_blocks(self):
        self.assertEqual(_verdict(versions_changed=True), "block")

    @given(
        discovery_complete=st.booleans(),
        published=id_sets,
        now=times,
        blocked_until=times,
        other_write_pending=st.booleans(),
        served=id_sets,
        known_dynamic=id_sets,
        versions_changed=st.booleans(),
    )
    def test_verdict_matches_its_rules(self, **kw):
        verdict = poll_verdict(**kw)
        gated = (
            not kw["discovery_complete"]
            or not kw["published"]
            or kw["now"] < kw["blocked_until"]
            or kw["other_write_pending"]
        )
        restart = bool(kw["published"] - kw["served"] - kw["known_dynamic"])
        if gated:
            self.assertEqual(verdict, "skip")
        elif restart or kw["versions_changed"]:
            self.assertEqual(verdict, "block")
        else:
            self.assertEqual(verdict, "ok")


class TestVersionChanged(unittest.TestCase):
    def test_same_versions(self):
        self.assertFalse(version_changed({2509: 1038}, {2509: 1038, 1: 0}, [2509]))

    def test_raised_version(self):
        self.assertTrue(version_changed({2509: 1038}, {2509: 1039}, [2509]))

    def test_version_point_no_longer_served(self):
        self.assertTrue(version_changed({2509: 1038}, {}, [2509]))

    def test_version_point_not_served_before_is_ignored(self):
        self.assertFalse(version_changed({}, {2509: 1039}, [2509]))

    def test_other_points_are_ignored(self):
        self.assertFalse(version_changed({7: 1}, {7: 2}, [2509]))


class TestChangedCandidates(unittest.TestCase):
    def test_changed_value_is_reported(self):
        self.assertEqual(changed_candidates([(5, False)], {5: 0}, {5: 1}), [(5, 0, 1)])

    def test_firmware_removed_candidate_is_skipped(self):
        self.assertEqual(changed_candidates([(5, True)], {5: 0}, {5: 1}), [])

    def test_a_skipped_candidate_does_not_hide_the_ones_after_it(self):
        """A removed or unchanged switch listed first must not end the scan."""
        self.assertEqual(
            changed_candidates([(5, True), (6, False)], {5: 0, 6: 0}, {5: 1, 6: 1}),
            [(6, 0, 1)],
        )
        self.assertEqual(
            changed_candidates([(5, False), (6, False)], {5: 1, 6: 0}, {5: 1, 6: 1}),
            [(6, 0, 1)],
        )

    def test_unchanged_or_unserved_is_not_reported(self):
        self.assertEqual(changed_candidates([(5, False)], {5: 1}, {5: 1}), [])
        self.assertEqual(changed_candidates([(5, False)], {}, {5: 1}), [])
        self.assertEqual(changed_candidates([(5, False)], {5: 1}, {}), [])
        self.assertEqual(changed_candidates([(5, False)], {5: 1}, {5: None}), [])

    @given(
        candidates=st.dictionaries(ids, st.booleans(), max_size=10),
        previous=st.dictionaries(ids, values, max_size=15),
        current=st.dictionaries(ids, values, max_size=15),
    )
    @example(candidates={5: True, 6: False}, previous={6: 0}, current={6: 1})
    @example(candidates={5: False, 6: False}, previous={5: 1, 6: 0}, current={5: 1, 6: 1})
    def test_reports_exactly_the_changed_candidates(self, candidates, previous, current):
        result = changed_candidates(candidates.items(), previous, current)
        expected = [
            (pid, previous[pid], current[pid])
            for pid, removed in candidates.items()
            if not removed
            and previous.get(pid) is not None
            and current.get(pid) is not None
            and previous[pid] != current[pid]
        ]
        self.assertEqual(result, expected)


class TestControllerSideDecision(unittest.TestCase):
    def test_unlearned_value_opens_a_window(self):
        self.assertEqual(
            controller_side_decision([(5, 0, 1)], {5: {1}}, set()),
            ControllerSide(5, 0, 1, open_window=True),
        )

    def test_learned_value_starts_a_late_arrival_period(self):
        self.assertEqual(
            controller_side_decision([(5, 0, 1)], {5: set()}, set()),
            ControllerSide(5, 0, 1, open_window=False),
        )

    def test_value_tried_this_run_opens_no_second_window(self):
        decision = controller_side_decision([(5, 0, 1)], {5: {1}}, {(5, 1)})
        self.assertFalse(decision.open_window)

    def test_no_change_or_several_changes_decide_nothing(self):
        self.assertIsNone(controller_side_decision([], {}, set()))
        self.assertIsNone(controller_side_decision([(5, 0, 1), (6, 1, 0)], {5: {1}}, set()))

    @given(
        changed=st.lists(st.tuples(ids, values, values), max_size=3),
        unprocessed=st.dictionaries(ids, st.frozensets(st.integers(0, 4)), max_size=5),
        tried=st.frozensets(st.tuples(ids, st.integers(0, 4)), max_size=5),
    )
    @example(changed=[(5, 0, 1)], unprocessed={5: frozenset({1})}, tried=frozenset({(5, 1)}))
    def test_opens_only_for_one_change_to_an_untried_unlearned_value(
        self, changed, unprocessed, tried
    ):
        decision = controller_side_decision(changed, unprocessed, tried)
        if len(changed) != 1:
            self.assertIsNone(decision)
            return
        pid, old, new = changed[0]
        self.assertEqual((decision.point_id, decision.old, decision.new), (pid, old, new))
        self.assertEqual(
            decision.open_window,
            new in unprocessed.get(pid, set()) and (pid, new) not in tried,
        )


def _late(**overrides):
    args = {
        "until": 200.0,
        "value": 1,
        "now": 100.0,
        "served": {1, 2, 3},
        "current_value": 1,
        "others_changed": False,
        "published": {1, 2},
        "baseline": set(),
        "known_dynamic": set(),
    }
    args.update(overrides)
    return late_arrival_decision(**args)


class TestLateArrivalDecision(unittest.TestCase):
    def test_new_point_is_linked_and_ends_the_period(self):
        decision = _late()
        self.assertEqual(decision.new_points, [3])
        self.assertIs(decision.keep_tail, False)

    def test_nothing_new_keeps_the_period(self):
        decision = _late(served={1, 2})
        self.assertEqual((decision.keep_tail, decision.new_points), (True, []))

    def test_unattributable_poll_keeps_the_period(self):
        decision = _late(served=None)
        self.assertEqual((decision.keep_tail, decision.new_points), (True, []))

    def test_expired_period_ends(self):
        decision = _late(now=200.5)
        self.assertEqual((decision.keep_tail, decision.new_points), (False, []))

    def test_period_ends_at_its_edge_only_after_it(self):
        self.assertEqual(_late(now=200.0).new_points, [3])

    def test_switch_changed_back_ends_the_period(self):
        decision = _late(current_value=0)
        self.assertEqual((decision.keep_tail, decision.new_points), (False, []))

    def test_other_candidate_changed_ends_the_period(self):
        decision = _late(others_changed=True)
        self.assertEqual((decision.keep_tail, decision.new_points), (False, []))

    def test_indexed_or_known_dynamic_points_are_not_new(self):
        self.assertEqual(_late(baseline={3}).new_points, [])
        self.assertEqual(_late(known_dynamic={3}).new_points, [])

    @given(
        served=st.one_of(st.none(), id_sets),
        published=id_sets,
        baseline=id_sets,
        known_dynamic=id_sets,
        current_value=values,
        others_changed=st.booleans(),
        now=times,
        until=times,
    )
    def test_linked_points_are_new_and_end_the_period(self, **kw):
        decision = late_arrival_decision(value=1, **kw)
        new = set(decision.new_points)
        self.assertEqual(decision.new_points, sorted(new))
        if new:
            self.assertFalse(decision.keep_tail)
            self.assertLessEqual(kw["now"], kw["until"])
            self.assertFalse(kw["others_changed"])
            self.assertEqual(kw["current_value"], 1)
            self.assertTrue(new <= kw["served"])
            self.assertFalse(new & (kw["published"] | kw["baseline"] | kw["known_dynamic"]))
        if kw["now"] > kw["until"]:
            self.assertFalse(decision.keep_tail)


class TestLearningOutcome(unittest.TestCase):
    @given(saw_points=st.booleans(), blocked=st.booleans())
    def test_value_learned_in_the_window_stays_learned(self, saw_points, blocked):
        self.assertEqual(
            learning_outcome(unprocessed=False, saw_points=saw_points, blocked=blocked),
            "learned",
        )

    def test_quiet_unblocked_window_records(self):
        self.assertEqual(
            learning_outcome(unprocessed=True, saw_points=False, blocked=False), "record"
        )

    def test_window_proving_nothing_skips(self):
        self.assertEqual(learning_outcome(unprocessed=True, saw_points=True, blocked=False), "skip")
        self.assertEqual(learning_outcome(unprocessed=True, saw_points=False, blocked=True), "skip")


class TestRecordableValue(unittest.TestCase):
    def test_value_the_controller_still_reports(self):
        self.assertEqual(recordable_value(1, 1), 1)

    def test_switch_already_reads_another_value(self):
        self.assertIsNone(recordable_value(1, 0))

    def test_ambiguous_or_unknown_window(self):
        self.assertIsNone(recordable_value(None, 1))

    def test_switch_not_served(self):
        self.assertIsNone(recordable_value(1, None))

    @given(window=values, current=values)
    def test_records_only_a_known_value_still_reported(self, window, current):
        result = recordable_value(window, current)
        if result is not None:
            self.assertEqual(result, window)
            self.assertEqual(result, current)
        else:
            self.assertTrue(window is None or window != current)


class TestLateTailUntil(unittest.TestCase):
    def test_adds_window_period_and_one_poll(self):
        self.assertEqual(late_tail_until(1000.0, 90, 180, 30), 1300.0)


if __name__ == "__main__":
    unittest.main()
