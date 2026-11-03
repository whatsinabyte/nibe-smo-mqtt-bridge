"""
test_utils.py
=============
Nibe_utils + cross-cutting tests.
Part of the Nibe S-Series MQTT Bridge test suite.
Shared fixtures are in conftest.py.
"""

import os
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


class TestAtomicWriteText(unittest.TestCase):
    """atomic_write_text: every /data fallback file is read back with its
    parse failure swallowed as "no data", so a half-written file silently
    discards whatever it held rather than surfacing an error. Being killed
    mid-write is a real scenario here — the shutdown drain budget can exceed
    a supervisor's stop grace period — so the destination must only ever
    hold complete contents."""

    def setUp(self):
        import tempfile

        self._dir = tempfile.mkdtemp()
        self._path = os.path.join(self._dir, "state.json")

    def tearDown(self):
        import shutil

        shutil.rmtree(self._dir, ignore_errors=True)

    def _read(self):
        with open(self._path, encoding="utf-8") as f:
            return f.read()

    def test_writes_the_payload(self):
        from nibe_utils import atomic_write_text

        atomic_write_text(self._path, "hello")
        self.assertEqual(self._read(), "hello")

    def test_overwrites_existing_contents_completely(self):
        from nibe_utils import atomic_write_text

        atomic_write_text(self._path, "a-much-longer-original-payload")
        atomic_write_text(self._path, "short")
        self.assertEqual(self._read(), "short")

    def test_failed_write_leaves_previous_contents_intact(self):
        """The point of the temp-and-rename: a write that dies partway
        must not destroy what was already there."""
        from nibe_utils import atomic_write_text

        atomic_write_text(self._path, "original")
        with (
            patch("nibe_utils.os.replace", side_effect=OSError("boom")),
            self.assertRaises(OSError),
        ):
            atomic_write_text(self._path, "replacement")
        self.assertEqual(self._read(), "original")

    def test_concurrent_writers_never_produce_a_corrupt_file(self):
        """None of the callers hold a common lock, and they run on the MQTT
        command thread, the management executor, the registry watcher thread
        and the poll thread. Sharing one fixed temp path let two writers
        truncate each other's half-written file and rename the mixture over
        the destination — the corruption the atomic write exists to prevent.
        Whichever writer lands last may win; what must never happen is a
        destination that parses as neither payload."""
        import json as _json
        import threading as _threading

        from nibe_utils import atomic_write_text

        # Payloads of very different lengths, so a truncating collision shows
        # up as trailing remnants of the longer one rather than clean JSON.
        payloads = [_json.dumps([i] * (200 * (i + 1))) for i in range(6)]
        errors: list[BaseException] = []
        barrier = _threading.Barrier(len(payloads))

        def writer(payload: str) -> None:
            try:
                barrier.wait(timeout=10)
                for _ in range(20):
                    atomic_write_text(self._path, payload)
            except BaseException as e:  # noqa: BLE001 — surfaced via `errors`
                errors.append(e)

        threads = [_threading.Thread(target=writer, args=(p,)) for p in payloads]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [], f"writer threads raised: {errors}")
        self.assertIn(
            self._read(), payloads, "destination held neither payload intact — a torn write"
        )

    def test_temp_files_are_not_left_behind_by_concurrent_writers(self):
        from nibe_utils import atomic_write_text

        for _ in range(5):
            atomic_write_text(self._path, "payload")
        leftovers = [n for n in os.listdir(self._dir) if n != os.path.basename(self._path)]
        self.assertEqual(leftovers, [], f"temp files left behind: {leftovers}")

    def test_failed_write_leaves_no_temp_file_behind(self):
        from nibe_utils import atomic_write_text

        with (
            patch("nibe_utils.os.replace", side_effect=OSError("boom")),
            self.assertRaises(OSError),
        ):
            atomic_write_text(self._path, "replacement")
        self.assertFalse(os.path.exists(self._path + ".tmp"))

    def test_oserror_propagates_so_callers_can_log_it(self):
        from nibe_utils import atomic_write_text

        missing = os.path.join(self._dir, "no-such-dir", "state.json")
        with self.assertRaises(OSError):
            atomic_write_text(missing, "x")

    def test_a_bare_file_name_is_written_in_the_current_directory(self):
        from nibe_utils import atomic_write_text

        cwd = os.getcwd()
        os.chdir(self._dir)
        try:
            atomic_write_text("bare.json", "x")
        finally:
            os.chdir(cwd)
        with open(os.path.join(self._dir, "bare.json"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "x")

    def test_temp_file_is_a_unique_sibling(self):
        """A unique temp file next to the destination: same filesystem, so
        the rename is atomic, and no two writers share one."""
        from nibe_utils import atomic_write_text

        real_replace = os.replace
        sources = []

        def spy_replace(src, dst):
            sources.append((src, dst))
            return real_replace(src, dst)

        with patch("nibe_utils.os.replace", side_effect=spy_replace):
            atomic_write_text(self._path, "one")
            atomic_write_text(self._path, "two")
        (src1, dst1), (src2, dst2) = sources
        self.assertEqual((dst1, dst2), (self._path, self._path))
        for src in (src1, src2):
            self.assertEqual(os.path.dirname(src), self._dir)
            self.assertTrue(os.path.basename(src).startswith(".state.json."))
            self.assertTrue(src.endswith(".tmp"))
        self.assertNotEqual(src1, src2)

    def test_file_keeps_the_permissions_a_plain_open_gave(self):
        """mkstemp creates the file 0600; anything reading these files
        expects the 0644 a plain open() left them with."""
        from nibe_utils import atomic_write_text

        atomic_write_text(self._path, "x")
        self.assertEqual(os.stat(self._path).st_mode & 0o777, 0o644)

    def test_a_failing_cleanup_does_not_hide_the_write_error(self):
        """When the temp file can't be removed either, the caller still gets
        the write's own OSError, not the cleanup's."""
        from nibe_utils import atomic_write_text

        with (
            patch("nibe_utils.os.replace", side_effect=OSError("disk full")),
            patch("nibe_utils.os.unlink", side_effect=OSError("busy")),
            self.assertRaises(OSError) as ctx,
        ):
            atomic_write_text(self._path, "x")
        self.assertEqual(str(ctx.exception), "disk full")
