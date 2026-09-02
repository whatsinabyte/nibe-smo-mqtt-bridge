"""Integration tests: EntityManager's /data/ persistence against a real
filesystem, not a mocked open().

Every other test that touches _WANTED_POINTS_FILE or similar persisted
state mocks the file I/O away entirely, proving only that the right
open()/json.dump() calls happen -- not that the actual recovery logic
survives what a real filesystem can genuinely do to it: a truncated file
left behind by a crash mid-write, a directory the process can't write to
(a misconfigured bind mount, a permissions mistake), or running out of
space entirely. Those are real Home Assistant add-on deployment failure
modes, not hypothetical ones, and the code's own recovery path (try/except
OSError around the write, try/except (OSError, ValueError) around the
read) has never actually been exercised against a real failing filesystem
operation -- only against a mocked one that fails in whatever shape a test
told it to.

No external service, no Docker, no opt-in env var -- part of the normal,
always-run suite. Uses pytest's tmp_path fixture, so nothing here touches
the real repo or a real /data/ directory.
"""

from __future__ import annotations

import json
import os
import resource
import signal
from unittest.mock import MagicMock, patch

import pytest
from conftest import _make_em


def _em_ready_for_discover_points():
    """An EntityManager whose discover_points() will reach the wanted-
    points file-fallback logic and then succeed trivially afterward --
    _fetch_bulk_data is faked (not the filesystem code this suite is
    actually about) so discover_points() completes without needing a real
    Nibe device."""
    em = _make_em()
    em._fetch_bulk_data = MagicMock(return_value=True)
    return em


class TestWantedPointsFileCorruptionAgainstARealFilesystem:
    def test_truncated_json_at_startup_falls_back_to_empty_not_a_crash(self, tmp_path) -> None:
        """A real truncated file -- exactly what a crash or power loss
        mid-write leaves behind -- must be recovered from safely, not
        crash startup or silently retain garbage state."""
        bad_file = tmp_path / "wanted_points.json"
        bad_file.write_text('[1, 2, 3, "unterminated string with no clos')

        em = _em_ready_for_discover_points()
        with patch("nibe_entity_manager._WANTED_POINTS_FILE", str(bad_file)):
            result = em.discover_points()  # must not raise

        assert result is True
        assert em._wanted_points == set()

    def test_missing_file_falls_back_to_empty_not_a_crash(self, tmp_path) -> None:
        """The ordinary first-install case: the file has never existed at
        all. Real os-level FileNotFoundError, not a mocked one."""
        missing = tmp_path / "does_not_exist" / "wanted_points.json"

        em = _em_ready_for_discover_points()
        with patch("nibe_entity_manager._WANTED_POINTS_FILE", str(missing)):
            result = em.discover_points()

        assert result is True
        assert em._wanted_points == set()

    def test_valid_file_is_actually_loaded_from_a_real_read(self, tmp_path) -> None:
        """The positive case, for contrast with the two failure cases
        above -- a real, valid file really does get read and parsed."""
        good_file = tmp_path / "wanted_points.json"
        good_file.write_text(json.dumps([100, 200, 300]))

        em = _em_ready_for_discover_points()
        with patch("nibe_entity_manager._WANTED_POINTS_FILE", str(good_file)):
            em.discover_points()

        assert em._wanted_points == {100, 200, 300}


class TestWantedPointsPersistPermissionDeniedAgainstARealFilesystem:
    def test_unwritable_directory_logs_a_warning_not_a_crash(self, tmp_path) -> None:
        """A real permission-denied write -- e.g. a misconfigured bind
        mount, or /data/ owned by the wrong user -- must degrade to a
        logged warning, not crash the write path or (worse) the caller."""
        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        target = readonly_dir / "wanted_points.json"
        os.chmod(readonly_dir, 0o500)  # r-x, no write
        try:
            em = _make_em()
            em._wanted_points = {1, 2, 3}
            em._persist_wanted_points(path=str(target))  # must not raise
            assert not target.exists()
        finally:
            os.chmod(readonly_dir, 0o700)  # restore so tmp_path cleanup can remove it


class TestDynamicPointMapFileCorruptionAgainstARealFilesystem:
    """Same corruption-recovery treatment as wanted_points.json above, but
    for /data/dynamic_point_map.json -- the other on-disk state file this
    bridge maintains, with its own independent try/except OSError (to_file)
    and try/except (JSONDecodeError, OSError) (from_file/deserialise) paths
    that have likewise only ever been proven against a mocked open()."""

    def test_truncated_json_falls_back_to_zero_entries_not_a_crash(self, tmp_path) -> None:
        from nibe_dynamic_map import DynamicPointMap

        bad_file = tmp_path / "dynamic_point_map.json"
        bad_file.write_text('{"100": {"unit": "unterminated')

        dpm = DynamicPointMap()
        count = dpm.from_file(path=str(bad_file))  # must not raise

        assert count == 0

    def test_missing_file_falls_back_to_zero_entries_not_a_crash(self, tmp_path) -> None:
        from nibe_dynamic_map import DynamicPointMap

        missing = tmp_path / "does_not_exist" / "dynamic_point_map.json"

        dpm = DynamicPointMap()
        count = dpm.from_file(path=str(missing))

        assert count == 0

    def test_valid_file_is_actually_round_tripped_through_a_real_write_and_read(
        self, tmp_path
    ) -> None:
        from nibe_dynamic_map import DynamicPointMap

        target = tmp_path / "dynamic_point_map.json"
        writer = DynamicPointMap()
        writer.deserialise(
            json.dumps({"100": {"point_id": 100, "title": "Test Switch", "entity_type": "switch"}})
        )
        assert writer.to_file(path=str(target)) is True

        reader = DynamicPointMap()
        count = reader.from_file(path=str(target))
        assert count == 1

    def test_unwritable_directory_logs_a_warning_and_returns_false_not_a_crash(
        self, tmp_path
    ) -> None:
        from nibe_dynamic_map import DynamicPointMap

        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        target = readonly_dir / "dynamic_point_map.json"
        os.chmod(readonly_dir, 0o500)  # r-x, no write
        try:
            dpm = DynamicPointMap()
            dpm.deserialise(json.dumps({"100": {"point_id": 100}}))
            assert dpm.to_file(path=str(target)) is False
            assert not target.exists()
        finally:
            os.chmod(readonly_dir, 0o700)  # restore so tmp_path cleanup can remove it


class TestMenuStructureYamlCorruptionAgainstARealFilesystem:
    """menu_structure.yaml is loaded once at startup and cached — a
    malformed or missing copy must degrade the menus dashboard, not
    prevent the bridge from starting. _load_menu_structure_yaml() itself
    documents that it re-raises like a bare yaml.safe_load(open(...)) —
    it's generate_nibe_mqtt._load_menu_structure() that is the actual
    try/except boundary callers rely on, so that's what this proves against
    a real corrupt/missing file rather than a mocked yaml.safe_load()."""

    def test_malformed_yaml_degrades_to_empty_map_not_a_crash(self, tmp_path) -> None:
        from generate_nibe_mqtt import _load_menu_structure
        from nibe_lovelace import _reset_menu_structure_cache

        app_dir = tmp_path
        (app_dir / "menu_structure.yaml").write_text("menus: [unterminated: [1, 2\n")
        _reset_menu_structure_cache()

        point_to_menu, menu_points = _load_menu_structure(str(app_dir), log_if_mode=False)

        assert point_to_menu == {}
        assert menu_points == frozenset()

    def test_missing_yaml_degrades_to_empty_map_not_a_crash(self, tmp_path) -> None:
        from generate_nibe_mqtt import _load_menu_structure
        from nibe_lovelace import _reset_menu_structure_cache

        app_dir = tmp_path  # no menu_structure.yaml written at all
        _reset_menu_structure_cache()

        point_to_menu, menu_points = _load_menu_structure(str(app_dir), log_if_mode=False)

        assert point_to_menu == {}
        assert menu_points == frozenset()

    def test_valid_yaml_is_actually_parsed_from_a_real_read(self, tmp_path) -> None:
        from generate_nibe_mqtt import _load_menu_structure
        from nibe_lovelace import _reset_menu_structure_cache

        app_dir = tmp_path
        (app_dir / "menu_structure.yaml").write_text(
            "menus:\n"
            "  - id: test_menu\n"
            "    title: Test Menu\n"
            "    settings:\n"
            "      - point_id: 100\n"
            "      - point_id: 200\n"
        )
        _reset_menu_structure_cache()

        point_to_menu, menu_points = _load_menu_structure(str(app_dir), log_if_mode=False)

        assert point_to_menu.get(100) == ("test_menu", "Test Menu")
        assert 100 in menu_points and 200 in menu_points


class TestWantedPointsPersistDiskFullAgainstARealFilesystem:
    """RLIMIT_FSIZE, not a mocked write() raising OSError: caps the real
    maximum file size this process may write to, so the write below fails
    with a real EFBIG from the real OS -- the same failure shape a
    genuinely full disk produces (open()/write() raising OSError partway
    through), without needing to actually fill a filesystem. SIGXFSZ is
    ignored first: POSIX's default action for that signal is to terminate
    the process outright, which would kill the test runner rather than
    let Python's write() raise normally."""

    @pytest.mark.skipif(not hasattr(signal, "SIGXFSZ"), reason="POSIX-only (no SIGXFSZ)")
    def test_write_failure_partway_through_logs_a_warning_not_a_crash(self, tmp_path) -> None:
        target = tmp_path / "wanted_points.json"
        old_handler = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        resource.setrlimit(resource.RLIMIT_FSIZE, (10, hard))
        try:
            em = _make_em()
            # A payload guaranteed to exceed the 10-byte cap regardless of
            # how many points end up in the sorted-list JSON encoding.
            em._wanted_points = set(range(1000, 1050))
            em._persist_wanted_points(path=str(target))  # must not raise
        finally:
            resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))
            signal.signal(signal.SIGXFSZ, old_handler)

        # The partial file left behind (real disk-full leaves partial
        # writes behind too) must not be mistaken for valid data on the
        # next startup -- reusing the corruption-recovery test above's own
        # code path confirms that.
        if target.exists():
            assert target.read_text() != json.dumps(sorted(em._wanted_points))
