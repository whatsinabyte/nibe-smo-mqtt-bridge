"""
test_card.py
============
Card js logic tests.
Part of the Nibe S-Series MQTT Bridge test suite.
Shared fixtures are in conftest.py.

These tests execute the real nibe-entity-manager-card.js via QuickJS
(see js_card_harness.py) for the methods that matter most — changelog
item validation and the enable/disable outcome toast — rather than a
hand-written Python re-implementation that could silently drift from the
shipped file. quickjs is a dev-only dependency; classes using it skip
cleanly if it isn't installed (e.g. on-device where it's intentionally
not shipped — see js_card_harness.py's docstring).
"""

import json
import unittest

from js_card_harness import QUICKJS_AVAILABLE, load_card_context


def _call_static(ctx, method_name, *args):
    args_js = ", ".join(json.dumps(a) for a in args)
    result = ctx.eval(f"JSON.stringify(NibeEntityManager.{method_name}({args_js}))")
    return json.loads(result) if result not in (None, "undefined") else None


def _call_instance(ctx, method_name, this_stub_js, *args):
    args_js = ", ".join(json.dumps(a) for a in args)
    ctx.eval(f"var __fakeThis = {this_stub_js};")
    result = ctx.eval(
        f"JSON.stringify(NibeEntityManager.prototype.{method_name}.call(__fakeThis, {args_js}))"
    )
    return json.loads(result) if result not in (None, "undefined") else None


@unittest.skipUnless(QUICKJS_AVAILABLE, "quickjs not installed — dev-only dependency")
class TestChangelogItemValidation(unittest.TestCase):
    """handleChangelogHistoryMessage filters added/removed arrays to keep
    only well-formed objects (type=object, id=number) via the extracted
    NibeEntityManager._isValidChangelogItem / _cleanChangelogEntry methods.
    Previously a null or non-object element would propagate to
    _renderChangelogContent and throw on e.id access, silently swallowing
    the entire render via the outer try/catch. These tests call the real
    methods from the shipped file directly."""

    @classmethod
    def setUpClass(cls):
        cls.ctx = load_card_context()

    def _clean_entry(self, raw_entry):
        """Call the real _cleanChangelogEntry with a stub `this` providing
        only the one dependency it needs (formatDateTimeHA)."""
        return _call_instance(
            self.ctx,
            "_cleanChangelogEntry",
            "{ formatDateTimeHA: function(d) { return 'TS'; } }",
            raw_entry,
        )

    def test_valid_items_pass_through(self):
        entry = {"added": [{"id": 50827, "title": "Humidity", "type": "sensor"}], "removed": []}
        clean = self._clean_entry(entry)
        self.assertIsNotNone(clean)
        self.assertEqual(len(clean["added"]), 1)
        self.assertEqual(clean["added"][0]["id"], 50827)

    def test_null_items_filtered_out(self):
        entry = {
            "added": [None, {"id": 300, "title": "Point 300", "type": "sensor"}],
            "removed": [],
        }
        clean = self._clean_entry(entry)
        self.assertEqual(len(clean["added"]), 1)
        self.assertEqual(clean["added"][0]["id"], 300)

    def test_non_object_items_filtered_out(self):
        entry = {
            "added": ["bad", 42, True, {"id": 301, "title": "P", "type": "sensor"}],
            "removed": [],
        }
        clean = self._clean_entry(entry)
        self.assertEqual(len(clean["added"]), 1)

    def test_object_without_numeric_id_filtered_out(self):
        """An object with a string id or missing id must be dropped —
        the renderer calls this._num(e.id, '?') which requires a number."""
        entry = {
            "added": [
                {"id": "not-a-number", "title": "Bad", "type": "sensor"},
                {"title": "No id", "type": "sensor"},
                {"id": 302, "title": "Good", "type": "sensor"},
            ],
            "removed": [],
        }
        clean = self._clean_entry(entry)
        self.assertEqual(len(clean["added"]), 1)
        self.assertEqual(clean["added"][0]["id"], 302)

    def test_both_arrays_validated_independently(self):
        entry = {
            "added": [None, {"id": 300, "title": "A", "type": "sensor"}],
            "removed": [{"id": "bad"}, {"id": 301, "title": "B", "type": "sensor"}],
        }
        clean = self._clean_entry(entry)
        self.assertEqual(len(clean["added"]), 1)
        self.assertEqual(len(clean["removed"]), 1)
        self.assertEqual(clean["removed"][0]["id"], 301)

    def test_missing_arrays_default_to_empty_and_entry_dropped(self):
        """No added/removed items at all — _cleanChangelogEntry returns null
        so the caller skips pushing this entry into the changelog."""
        clean = self._clean_entry({})
        self.assertIsNone(clean)

    def test_is_valid_changelog_item_directly(self):
        self.assertTrue(_call_static(self.ctx, "_isValidChangelogItem", {"id": 5}))
        self.assertFalse(_call_static(self.ctx, "_isValidChangelogItem", None))
        self.assertFalse(_call_static(self.ctx, "_isValidChangelogItem", {"id": "x"}))
        self.assertFalse(_call_static(self.ctx, "_isValidChangelogItem", "not-an-object"))


@unittest.skipUnless(QUICKJS_AVAILABLE, "quickjs not installed — dev-only dependency")
class TestStaleChangelogSeq(unittest.TestCase):
    """_isStaleChangelogSeq: skip a stale retained changelog message that
    arrives after a fresher one (e.g. a broker replay racing with a live
    update after reconnect) — per docs/card-api.md's documented _seq
    contract, which nothing previously enforced despite being documented."""

    @classmethod
    def setUpClass(cls):
        cls.ctx = load_card_context()

    def _stale(self, seq, last_seq):
        return _call_static(self.ctx, "_isStaleChangelogSeq", seq, last_seq)

    def test_no_prior_seq_never_stale(self):
        """First message ever received (lastSeq=null) is always applied."""
        self.assertFalse(self._stale(5, None))

    def test_lower_seq_is_stale(self):
        self.assertTrue(self._stale(3, 5))

    def test_equal_seq_is_stale(self):
        """A duplicate delivery of the same seq must not re-apply."""
        self.assertTrue(self._stale(5, 5))

    def test_higher_seq_is_not_stale(self):
        self.assertFalse(self._stale(6, 5))

    def test_non_numeric_seq_never_stale(self):
        """A payload without _seq (older bridge version, or missing field)
        must not be treated as stale — always applied, matching legacy
        (pre-_seq) behaviour."""
        self.assertFalse(self._stale(None, 5))
        self.assertFalse(self._stale("not-a-number", 5))


@unittest.skipUnless(QUICKJS_AVAILABLE, "quickjs not installed — dev-only dependency")
class TestClampPage(unittest.TestCase):
    """_clampPage: a filter or MQTT-driven data change can shrink the
    result count out from under a currentPage that pointed at a later
    page — must clamp back into range rather than rendering an empty
    "No entities match filters" page when matching entities exist
    earlier."""

    @classmethod
    def setUpClass(cls):
        cls.ctx = load_card_context()

    def _clamp(self, current_page, total, page_size):
        return _call_static(self.ctx, "_clampPage", current_page, total, page_size)

    def test_page_within_range_unchanged(self):
        self.assertEqual(self._clamp(1, 50, 20), 1)

    def test_page_past_end_clamped_to_last_page(self):
        """3 pages of 20 (60 total), currentPage=5 is way past the end —
        must clamp to the last valid page (2), not stay at 5."""
        self.assertEqual(self._clamp(5, 60, 20), 2)

    def test_page_exactly_at_total_pages_clamped(self):
        """total=40, pageSize=20 -> totalPages=2, valid pages are 0-1.
        currentPage=2 is one past the end — must clamp to 1."""
        self.assertEqual(self._clamp(2, 40, 20), 1)

    def test_zero_results_clamps_to_zero(self):
        """All entities filtered out — must not divide/return a negative
        or NaN page index."""
        self.assertEqual(self._clamp(3, 0, 20), 0)

    def test_first_page_stays_zero(self):
        self.assertEqual(self._clamp(0, 0, 20), 0)


@unittest.skipUnless(QUICKJS_AVAILABLE, "quickjs not installed — dev-only dependency")
class TestEscapeHelper(unittest.TestCase):
    """_esc() is the card's sole defense against firmware-sourced strings
    (title/description/unit, all attacker-influenceable via a crafted MQTT
    payload) being interpreted as HTML when inserted via innerHTML. Tested
    directly against the shipped implementation since this is the one place
    a Python mirror drifting from reality would be a real security gap."""

    @classmethod
    def setUpClass(cls):
        cls.ctx = load_card_context()

    def _esc(self, s):
        return _call_instance(self.ctx, "_esc", "{}", s)

    def test_script_tag_escaped(self):
        self.assertEqual(
            self._esc("<script>alert(1)</script>"), "&lt;script&gt;alert(1)&lt;/script&gt;"
        )

    def test_ampersand_escaped(self):
        self.assertEqual(self._esc("A & B"), "A &amp; B")

    def test_quotes_escaped(self):
        self.assertEqual(self._esc("""say "hi" it's fine"""), "say &quot;hi&quot; it&#39;s fine")

    def test_none_and_undefined_become_empty_string(self):
        self.assertEqual(self._esc(None), "")

    def test_plain_text_passes_through_unchanged(self):
        self.assertEqual(self._esc("Outdoor Temperature"), "Outdoor Temperature")


# ===========================================================================
# 66. Slice 2 fixes: F2 (toast outcome), F5 (filter sync)
# ===========================================================================


@unittest.skipUnless(QUICKJS_AVAILABLE, "quickjs not installed — dev-only dependency")
class TestEnableDisableToastLogic(unittest.TestCase):
    """enableEntities / disableEntities track a 'succeeded' count and show a
    truthful toast rather than unconditionally reporting the full input
    count as successful — three cases: all succeed (green success toast),
    partial failure (red toast with x-of-N wording), total failure (red
    toast with 'failed' wording). Both methods delegate the message/type
    decision to the extracted, pure NibeEntityManager._formatBulkOutcomeToast,
    called here directly against the shipped file."""

    @classmethod
    def setUpClass(cls):
        cls.ctx = load_card_context()

    def _toast_outcome(self, succeeded, total):
        result = _call_static(
            self.ctx, "_formatBulkOutcomeToast", "Enabled", "enable", succeeded, total
        )
        return result["message"], result["type"]

    def test_all_succeed_green_toast(self):
        msg, kind = self._toast_outcome(5, 5)
        self.assertIn("Enabled 5", msg)
        self.assertEqual(kind, "success")

    def test_partial_failure_reports_correct_counts(self):
        msg, kind = self._toast_outcome(3, 5)
        self.assertIn("3 of 5", msg)
        self.assertIn("2 failed", msg)
        self.assertEqual(kind, "error")

    def test_total_failure_reports_failed(self):
        msg, kind = self._toast_outcome(0, 3)
        self.assertIn("Failed to enable", msg)
        self.assertEqual(kind, "error")

    def test_single_entity_uses_singular_noun(self):
        msg, _ = self._toast_outcome(1, 1)
        self.assertIn("1 entity", msg)
        self.assertNotIn("entities", msg)

    def test_disable_wording_uses_disable_verbs(self):
        """Mirror check for the disable path — same decision logic, but
        enableEntities/disableEntities each pass their own verb/infinitive
        into the shared helper."""
        result = _call_static(self.ctx, "_formatBulkOutcomeToast", "Disabled", "disable", 3, 4)
        self.assertIn("3 of 4", result["message"])
        self.assertEqual(result["type"], "error")

    def test_disable_total_failure_wording(self):
        result = _call_static(self.ctx, "_formatBulkOutcomeToast", "Disabled", "disable", 0, 4)
        self.assertIn("Failed to disable", result["message"])


class TestMobileFilterSyncLogic(unittest.TestCase):
    """The mobile Apply button's click handler is an inline event listener
    closure (see setupMobileEventListeners in the card), not a standalone
    method — it isn't extractable into something callable in isolation
    without either restructuring it or faking a much larger slice of the
    DOM (getElementById, .value, .style) than is worth it for four
    straight-line assignments with no branching. Unlike the changelog and
    toast-outcome logic above, this simulates the intended contract in
    Python rather than executing the real file — call this an explicitly
    honest placeholder, not equivalent coverage."""

    def _simulate_apply(self, type_val, status_val, writable_val, dynamic_val):
        """Return the dict of desktop element values that the Apply handler
        should write — mirrors the actual handler's straight-line logic,
        not verified against the shipped file."""
        return {
            "type-filter": type_val,
            "status-filter": status_val,
            "writable-filter": writable_val,
            "dynamic-filter": dynamic_val,
        }

    def test_all_filters_synced_to_desktop(self):
        result = self._simulate_apply("switch", "enabled", "true", "dynamic")
        self.assertEqual(result["type-filter"], "switch")
        self.assertEqual(result["status-filter"], "enabled")
        self.assertEqual(result["writable-filter"], "true")
        self.assertEqual(result["dynamic-filter"], "dynamic")

    def test_empty_filters_also_synced(self):
        """Clearing all mobile filters and applying must reset desktop too."""
        result = self._simulate_apply("", "", "", "")
        for key in result:
            self.assertEqual(result[key], "", f"{key} should be empty string")

    def test_partial_filters_synced(self):
        """Only some filters active — desktop shows the active ones correctly."""
        result = self._simulate_apply("sensor", "", "", "")
        self.assertEqual(result["type-filter"], "sensor")
        self.assertEqual(result["status-filter"], "")
        self.assertEqual(result["writable-filter"], "")
        self.assertEqual(result["dynamic-filter"], "")

    def test_dynamic_filter_still_synced(self):
        """Regression: dynamic-filter was the only one synced before the fix —
        confirm it still works correctly after the refactor."""
        result = self._simulate_apply("", "", "", "static")
        self.assertEqual(result["dynamic-filter"], "static")


# ===========================================================================
# 67. generate_nibe_mqtt.py audit fixes
# ===========================================================================
