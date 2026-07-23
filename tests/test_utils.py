"""
test_utils.py
=============
Nibe_utils + cross-cutting tests.
Part of the Nibe S-Series MQTT Bridge test suite.
Shared fixtures are in conftest.py.
"""

import unittest

from hypothesis import given
from hypothesis import strategies as st

class TestFmtTsProperties(unittest.TestCase):
    """Hypothesis properties for fmt_ts."""

    @given(st.floats(min_value=0.0, max_value=2_000_000_000.0,
                     allow_nan=False, allow_infinity=False))
    def test_never_raises_for_valid_timestamps(self, t):
        from nibe_utils import fmt_ts
        result = fmt_ts(t)
        self.assertIsInstance(result, str)

    def test_none_returns_current_time_format(self):
        from nibe_utils import fmt_ts
        result = fmt_ts(None)
        # Must match YYYY-MM-DD HH:MM:SS
        self.assertRegex(result, r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')

    @given(st.floats(min_value=0.0, max_value=2_000_000_000.0,
                     allow_nan=False, allow_infinity=False))
    def test_output_format_is_always_datetime(self, t):
        from nibe_utils import fmt_ts
        result = fmt_ts(t)
        self.assertRegex(result, r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')



class TestFmtTsExtendedProperties(unittest.TestCase):
    """Extended Hypothesis properties for fmt_ts."""

    @given(st.floats(min_value=0.0, max_value=1_000_000_000.0,
                     allow_nan=False, allow_infinity=False),
           st.floats(min_value=0.0, max_value=1_000_000_000.0,
                     allow_nan=False, allow_infinity=False))
    def test_monotonic_with_timestamps(self, t1, t2):
        """fmt_ts is monotonic: if t1 ≤ t2 then fmt_ts(t1) ≤ fmt_ts(t2)
        (lexicographic, safe because YYYY-MM-DD HH:MM:SS is ISO-ordered)."""
        from nibe_utils import fmt_ts
        if t1 <= t2:
            self.assertLessEqual(fmt_ts(t1), fmt_ts(t2))


# ---------------------------------------------------------------------------
# Cross-function integration properties
# ---------------------------------------------------------------------------


