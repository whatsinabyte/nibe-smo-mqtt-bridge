"""
test_utils.py
=============
Nibe_utils + cross-cutting tests.
Part of the Nibe S-Series MQTT Bridge test suite.
Shared fixtures are in conftest.py.
"""

import unittest
from unittest.mock import patch

from hypothesis import given
from hypothesis import strategies as st


class TestFmtTsProperties(unittest.TestCase):
    """Hypothesis properties for fmt_ts."""

    @given(
        st.floats(min_value=0.0, max_value=2_000_000_000.0, allow_nan=False, allow_infinity=False)
    )
    def test_never_raises_for_valid_timestamps(self, t):
        from nibe_utils import fmt_ts

        result = fmt_ts(t)
        self.assertIsInstance(result, str)

    def test_none_returns_current_time_format(self):
        from nibe_utils import fmt_ts

        result = fmt_ts(None)
        # Must match YYYY-MM-DD HH:MM:SS
        self.assertRegex(result, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    @given(
        st.floats(min_value=0.0, max_value=2_000_000_000.0, allow_nan=False, allow_infinity=False)
    )
    def test_output_format_is_always_datetime(self, t):
        from nibe_utils import fmt_ts

        result = fmt_ts(t)
        self.assertRegex(result, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_given_timestamp_actually_used_not_ignored(self):
        """fmt_ts(t) must call time.localtime(t) with the REAL t — not
        silently ignore it and use the current-time branch instead. Every
        other test here only checks output FORMAT (regex), which a mutant
        that always uses the current time would still satisfy, including
        the monotonic-ordering property test (two current-time calls in
        the same second are trivially equal, satisfying <=)."""
        from nibe_utils import fmt_ts

        with (
            patch("nibe_utils.time.localtime") as mock_localtime,
            patch("nibe_utils.time.strftime", return_value="formatted"),
        ):
            fmt_ts(1234567890.0)
        mock_localtime.assert_called_once_with(1234567890.0)

    def test_none_uses_current_time_not_given_value(self):
        """fmt_ts(None) must call the zero-arg time.localtime() (current
        time) branch — not the t-argument branch."""
        from nibe_utils import fmt_ts

        with (
            patch("nibe_utils.time.localtime") as mock_localtime,
            patch("nibe_utils.time.strftime", return_value="formatted"),
        ):
            fmt_ts(None)
        mock_localtime.assert_called_once_with()


class TestFmtTsExtendedProperties(unittest.TestCase):
    """Extended Hypothesis properties for fmt_ts."""

    @given(
        st.floats(min_value=0.0, max_value=1_000_000_000.0, allow_nan=False, allow_infinity=False),
        st.floats(min_value=0.0, max_value=1_000_000_000.0, allow_nan=False, allow_infinity=False),
    )
    def test_monotonic_with_timestamps(self, t1, t2):
        """fmt_ts is monotonic: if t1 ≤ t2 then fmt_ts(t1) ≤ fmt_ts(t2)
        (lexicographic, safe because YYYY-MM-DD HH:MM:SS is ISO-ordered)."""
        from nibe_utils import fmt_ts

        if t1 <= t2:
            self.assertLessEqual(fmt_ts(t1), fmt_ts(t2))


# ---------------------------------------------------------------------------
# Cross-function integration properties
# ---------------------------------------------------------------------------
