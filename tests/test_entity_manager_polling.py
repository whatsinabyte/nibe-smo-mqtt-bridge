"""
test_entity_manager_polling.py
==============================
Bulk polling (_fetch_bulk_data, update_all_states) tests for nibe_entity_manager.py — split out of test_entity_manager.py
for file-size/maintainability. Shared fixtures are in conftest.py.
"""

import time
import unittest
from unittest.mock import patch

from conftest import (
    _make_em,
)


class TestFetchBulkDataStringCache(unittest.TestCase):
    """_fetch_bulk_data caches the clean_string() result for each point's
    title/description, only recomputing when the raw API string actually
    differs from what was cached last poll. A cache-invalidation bug here
    means either stale titles persisting after a firmware update (cache
    never invalidates) or needless CPU work every single poll cycle on a
    1000+ point installation (cache never hits) — covers only the cache
    behavior itself, not the rest of the (much larger) bulk-fetch function."""

    def _response(self, point_id=100, title='Outdoor temperature', description=''):
        return {
            str(point_id): {
                'title': title, 'description': description,
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                             'minValue': -400, 'maxValue': 400},
                'value': {'integerValue': 50, 'stringValue': '', 'isOk': True},
            }
        }

    def test_first_poll_populates_cache_and_calls_clean_string(self):
        em = _make_em()
        em._api.fetch_bulk_points.return_value = self._response()
        from nibe_entity_detection import clean_string as real_clean_string
        with patch('nibe_entity_manager.clean_string', wraps=real_clean_string) as spy:
            em._fetch_bulk_data(detect_changes=False)
        self.assertIn(100, em._point_string_cache)
        spy.assert_called()

    def test_unchanged_title_on_second_poll_skips_recompute(self):
        """Identical raw title/description on a second poll must hit the
        cache — clean_string must not be called again for this point."""
        em = _make_em()
        em._api.fetch_bulk_points.return_value = self._response()
        em._fetch_bulk_data(detect_changes=False)  # populate cache

        from nibe_entity_detection import clean_string as real_clean_string
        with patch('nibe_entity_manager.clean_string', wraps=real_clean_string) as spy:
            em._fetch_bulk_data(detect_changes=False)
        spy.assert_not_called()

    def test_changed_title_invalidates_cache_and_recomputes(self):
        """A genuinely different raw title (e.g. after a firmware update
        changes point naming) must invalidate the cache and recompute —
        confirms the cache doesn't silently freeze stale data forever."""
        em = _make_em()
        em._api.fetch_bulk_points.return_value = self._response(title='Old title')
        em._fetch_bulk_data(detect_changes=False)
        self.assertEqual(em.bulk_data[100]['title'], 'Old title')

        em._api.fetch_bulk_points.return_value = self._response(title='New title')
        em._fetch_bulk_data(detect_changes=False)
        self.assertEqual(em.bulk_data[100]['title'], 'New title')
        self.assertEqual(em._point_string_cache[100][0], 'New title')

    def test_changed_description_also_invalidates_cache(self):
        em = _make_em()
        em._api.fetch_bulk_points.return_value = self._response(description='Old desc')
        em._fetch_bulk_data(detect_changes=False)
        em._api.fetch_bulk_points.return_value = self._response(description='New desc')
        em._fetch_bulk_data(detect_changes=False)
        self.assertEqual(em.bulk_data[100]['description'], 'New desc')

    def test_disappeared_point_removed_from_cache(self):
        """A point that drops out of the bulk response entirely (e.g.
        accessory disconnected) must have its string cache entry cleaned
        up too, not just bulk_data — otherwise the cache grows unbounded
        with stale entries for points that no longer exist."""
        em = _make_em()
        em._api.fetch_bulk_points.return_value = self._response(point_id=100)
        em._fetch_bulk_data(detect_changes=False)
        self.assertIn(100, em._point_string_cache)

        em._api.fetch_bulk_points.return_value = self._response(point_id=200)  # 100 gone
        em._fetch_bulk_data(detect_changes=False)
        self.assertNotIn(100, em._point_string_cache)
        self.assertNotIn(100, em.bulk_data)

    def test_raw_value_updated_in_place_on_existing_entry(self):
        """bulk_data is updated in-place for an existing point (not
        rebuilt) — confirms the dict identity persists across polls while
        the value itself does update."""
        em = _make_em()
        em._api.fetch_bulk_points.return_value = self._response()
        em._fetch_bulk_data(detect_changes=False)
        entry_before = em.bulk_data[100]

        resp2 = self._response()
        resp2['100']['value']['integerValue'] = 99
        em._api.fetch_bulk_points.return_value = resp2
        em._fetch_bulk_data(detect_changes=False)

        self.assertIs(em.bulk_data[100], entry_before)  # same object, updated in place
        self.assertEqual(em.bulk_data[100]['raw_value'], 99)


class TestFetchBulkDataNewEntryFieldsCreatePath(unittest.TestCase):
    """_fetch_bulk_data's bulk_data[point_id] = {...} dict-creation branch
    (first poll for a given point_id, existing is None). Each field must
    be read from the correct API key, with the correct default when that
    key is absent — checked strictly on the very first poll only, since a
    second poll could mask a create-path bug via the separate update-path
    code that re-derives title/description/metadata independently."""

    def test_all_value_fields_read_from_correct_keys_on_first_poll(self):
        """Distinctive, non-default values for every value_data field must
        land in the matching bulk_data key on point creation — catches key
        name typos (e.g. 'stringValue' vs 'STRINGVALUE') that a test using
        only default/absent values could never observe."""
        em = _make_em()
        em._api.fetch_bulk_points.return_value = {
            '900': {
                'title': 'T', 'description': '',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                             'isWritable': False},
                'value': {'integerValue': 55, 'stringValue': 'SVAL', 'isOk': True},
            }
        }
        em._fetch_bulk_data(detect_changes=False)
        entry = em.bulk_data[900]
        self.assertEqual(entry['raw_value'], 55)
        self.assertEqual(entry['string_value'], 'SVAL')
        self.assertIs(entry['is_ok'], True)

    def test_is_ok_defaults_to_false_not_none_when_key_absent_on_first_poll(self):
        """value_data with no 'isOk' key at all must yield is_ok == False
        (the literal, not None) — assertFalse alone cannot distinguish the
        two, so assertIs is required."""
        em = _make_em()
        em._api.fetch_bulk_points.return_value = {
            '901': {
                'title': 'T', 'description': '',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                             'isWritable': False},
                'value': {'integerValue': 1, 'stringValue': ''},  # no isOk key
            }
        }
        em._fetch_bulk_data(detect_changes=False)
        self.assertIs(em.bulk_data[901]['is_ok'], False)

    def test_description_key_correct_on_first_poll_before_any_update_path_masking(self):
        """Checked strictly after ONE poll — a second poll's update-path
        (existing.get('description') != description) could independently
        fix a create-path key typo and mask the mutation."""
        em = _make_em()
        em._api.fetch_bulk_points.return_value = {
            '902': {
                'title': 'T', 'description': 'FirstPollDesc',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                             'isWritable': False},
                'value': {'integerValue': 1, 'stringValue': '', 'isOk': True},
            }
        }
        em._fetch_bulk_data(detect_changes=False)
        self.assertEqual(em.bulk_data[902]['description'], 'FirstPollDesc')

    def test_timestamp_set_to_the_now_value_captured_this_cycle_on_first_poll(self):
        """'timestamp' must be set to the actual `now` value computed at
        the top of the loop (a fresh time.time() call), not merely present
        under a typo'd key. Pin time.time() to a distinctive value."""
        em = _make_em()
        em._api.fetch_bulk_points.return_value = {
            '903': {
                'title': 'T', 'description': '',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                             'isWritable': False},
                'value': {'integerValue': 1, 'stringValue': '', 'isOk': True},
            }
        }
        with patch('nibe_entity_manager.time.time', return_value=424242.0):
            em._fetch_bulk_data(detect_changes=False)
        self.assertEqual(em.bulk_data[903]['timestamp'], 424242.0)


class TestFetchBulkDataStringCacheHitAssignsCorrectSlots(unittest.TestCase):
    """On a cache-hit poll (unchanged raw title/description), the code
    unpacks `title, description = cached_strings[2], cached_strings[3]`.
    A mutation that reads the same slot twice (e.g. both from index 3)
    would silently swap title and description content."""

    def test_title_and_description_not_swapped_on_cache_hit(self):
        em = _make_em()
        resp = {
            '904': {
                'title': 'AlphaTitle', 'description': 'BetaDescription',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                             'isWritable': False},
                'value': {'integerValue': 1, 'stringValue': '', 'isOk': True},
            }
        }
        em._api.fetch_bulk_points.return_value = resp
        em._fetch_bulk_data(detect_changes=False)  # populate cache
        first_title = em.bulk_data[904]['title']
        first_desc = em.bulk_data[904]['description']
        self.assertNotEqual(first_title, first_desc)  # sanity: distinct values

        # Second poll, identical raw strings -> hits the cache branch
        em._api.fetch_bulk_points.return_value = resp
        em._fetch_bulk_data(detect_changes=False)
        self.assertEqual(em.bulk_data[904]['title'], first_title)
        self.assertEqual(em.bulk_data[904]['description'], first_desc)


class TestFetchBulkDataMissingDescriptionKeyDefault(unittest.TestCase):
    """raw_desc = point_data.get('description', '') — when the API entry
    omits 'description' entirely, the default must be the empty string,
    not some other placeholder."""

    def test_missing_description_key_defaults_to_empty_string(self):
        em = _make_em()
        em._api.fetch_bulk_points.return_value = {
            '905': {
                'title': 'T',
                # no 'description' key at all
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                             'isWritable': False},
                'value': {'integerValue': 1, 'stringValue': '', 'isOk': True},
            }
        }
        em._fetch_bulk_data(detect_changes=False)
        self.assertEqual(em.bulk_data[905]['description'], '')


class TestFetchBulkDataEmptyDictResponseIsFailure(unittest.TestCase):
    """The response-validity check is `not response or not isinstance(...)`
    (OR). An empty dict {} is falsy but IS a dict, so only the OR form
    treats it as a failure — an AND mutant would let it through as a
    normal (zero-point) poll instead of calling _handle_api_failure."""

    def test_empty_dict_response_triggers_api_failure(self):
        em = _make_em()
        em._api.fetch_bulk_points.return_value = {}
        with patch.object(em, '_handle_api_failure') as mock_fail:
            result = em._fetch_bulk_data()
        self.assertIs(result, False)
        mock_fail.assert_called_once()


class TestFetchBulkDataUpdatePathFieldsCorrectKeys(unittest.TestCase):
    """The 'existing is not None' (update) branch re-reads raw_value,
    string_value, is_ok, timestamp from value_data on every subsequent
    poll. Verified separately from the create-path tests above since
    mutmut generates distinct mutants for each occurrence of these
    key/default literals."""

    def _resp(self, point_id, integer_value, string_value, is_ok):
        return {
            str(point_id): {
                'title': 'T', 'description': '',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                             'isWritable': False},
                'value': {'integerValue': integer_value,
                          'stringValue': string_value, 'isOk': is_ok},
            }
        }

    def test_update_path_reads_correct_keys_with_distinctive_values(self):
        em = _make_em()
        em._api.fetch_bulk_points.return_value = self._resp(910, 1, 'first', False)
        em._fetch_bulk_data(detect_changes=False)  # first poll -> create branch

        em._api.fetch_bulk_points.return_value = self._resp(910, 77, 'SECONDVAL', True)
        em._fetch_bulk_data(detect_changes=False)  # second poll -> update branch

        entry = em.bulk_data[910]
        self.assertEqual(entry['raw_value'], 77)
        self.assertEqual(entry['string_value'], 'SECONDVAL')
        self.assertIs(entry['is_ok'], True)

    def test_update_path_is_ok_defaults_to_false_not_none_when_key_absent(self):
        em = _make_em()
        em._api.fetch_bulk_points.return_value = self._resp(911, 1, '', True)
        em._fetch_bulk_data(detect_changes=False)  # first poll

        em._api.fetch_bulk_points.return_value = {
            '911': {
                'title': 'T', 'description': '',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                             'isWritable': False},
                'value': {'integerValue': 1, 'stringValue': ''},  # no isOk key
            }
        }
        em._fetch_bulk_data(detect_changes=False)  # second poll -> update branch
        self.assertIs(em.bulk_data[911]['is_ok'], False)

    def test_update_path_timestamp_set_to_now_captured_this_cycle(self):
        em = _make_em()
        em._api.fetch_bulk_points.return_value = self._resp(912, 1, '', True)
        em._fetch_bulk_data(detect_changes=False)  # first poll

        em._api.fetch_bulk_points.return_value = self._resp(912, 2, '', True)
        with patch('nibe_entity_manager.time.time', return_value=13579.0):
            em._fetch_bulk_data(detect_changes=False)  # second poll -> update branch
        self.assertEqual(em.bulk_data[912]['timestamp'], 13579.0)


class TestFetchBulkDataKnownDynamicPointRoutingCorrectness(unittest.TestCase):
    """A known dynamic point appearing outside baseline/published_configs
    must be routed through _publish_dynamic_changes's new_points list with
    the correct (point_id, point_data) tuple content — never indexed as a
    static permanent point (that would permanently misclassify it) and
    never appended as a bare None/placeholder."""

    def _resp(self, point_id):
        return {
            str(point_id): {
                'title': 'DynTitle', 'description': '',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                             'isWritable': False},
                'value': {'integerValue': 5, 'stringValue': '', 'isOk': True},
            }
        }

    def _setup_known_dynamic(self, em, controlling_id=77, dynamic_id=700):
        from nibe_dynamic_map import DynamicPointEntry
        em.dynamic_point_map._table[controlling_id] = DynamicPointEntry(
            point_id=controlling_id, title='Switch', entity_type='switch',
            processed_values={0, 1}, is_controlling=True,
            dynamic_points_by_value={1: [dynamic_id]},
        )

    def test_known_dynamic_point_outside_scan_not_indexed_as_static(self):
        """Correctly checking is_known_dynamic(point_id) (not some other/
        None argument) must keep this point OUT of all_points_by_id."""
        em = _make_em()
        em.initial_discovery_complete = True
        em.post_write_active = False
        self._setup_known_dynamic(em)
        resp = self._resp(700)
        em._api.fetch_bulk_points.return_value = resp
        with patch.object(em, '_publish_dynamic_changes') as mock_dyn:
            em._fetch_bulk_data(detect_changes=True)
        self.assertNotIn(700, em.all_points_by_id)
        mock_dyn.assert_called_once()
        new_points_arg = mock_dyn.call_args[0][0]
        self.assertEqual(new_points_arg, [(700, resp['700'])])

    def test_new_points_entry_is_id_data_tuple_during_post_write(self):
        """During a post-write scan, an appearing point must be appended to
        new_points as the literal (point_id, point_data) tuple — not a
        placeholder like None."""
        em = _make_em()
        em.initial_discovery_complete = True
        em.post_write_active = True
        em._post_write_until = time.time() + 9999
        resp = self._resp(701)
        em._api.fetch_bulk_points.return_value = resp
        with patch.object(em, '_publish_dynamic_changes') as mock_dyn:
            em._fetch_bulk_data(detect_changes=True)
        mock_dyn.assert_called_once()
        new_points_arg = mock_dyn.call_args[0][0]
        self.assertEqual(new_points_arg, [(701, resp['701'])])


class TestFetchBulkDataNewPermanentPointDetails(unittest.TestCase):
    """Fine-grained checks for the genuinely-new-permanent-point branch:
    the entity_type membership check that triggers populate_from_bulk, the
    exact dict content passed to it, and the args passed to the publish
    calls that follow."""

    def _resp(self, point_id):
        return {
            str(point_id): {
                'title': 'New Sensor', 'description': 'desc',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                             'isWritable': True},
                'value': {'integerValue': 5, 'stringValue': '', 'isOk': True},
            }
        }

    def test_select_entity_type_also_triggers_populate_from_bulk(self):
        """The membership check is `entity_type_p in ('switch', 'select')`
        — 'select' alone (not just 'switch') must also trigger it. Mock
        _get_cached_entity_type directly to avoid depending on the exact
        metadata shape that real classification would require."""
        em = _make_em()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em._api.fetch_bulk_points.return_value = self._resp(950)
        with (
            patch.object(em, '_get_cached_entity_type', return_value=('select', 'config')),
            patch.object(em.dynamic_point_map, 'populate_from_bulk') as mock_pop,
        ):
            em._fetch_bulk_data(detect_changes=True)
        mock_pop.assert_called_once()

    def test_populate_from_bulk_called_with_correct_point_id_and_entity_type(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em._api.fetch_bulk_points.return_value = self._resp(951)
        with (
            patch.object(em, '_get_cached_entity_type', return_value=('switch', 'config')),
            patch.object(em.dynamic_point_map, 'populate_from_bulk') as mock_pop,
        ):
            em._fetch_bulk_data(detect_changes=True)
        mock_pop.assert_called_once()
        args = mock_pop.call_args[0]
        points_arg, types_arg = args[0], args[1]
        self.assertIn(951, points_arg)
        self.assertEqual(types_arg, {951: 'switch'})

    def test_publish_point_metadata_called_with_the_indexed_point_object(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em._api.fetch_bulk_points.return_value = self._resp(952)
        em._fetch_bulk_data(detect_changes=True)
        expected_point_obj = em.all_points_by_id[952]
        em._pub.publish_point_metadata.assert_called_once_with(expected_point_obj)

    def test_publish_point_list_called_with_all_points_by_id_not_a_placeholder(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em._api.fetch_bulk_points.return_value = self._resp(953)
        em._fetch_bulk_data(detect_changes=True)
        em._pub.publish_point_list.assert_called_once_with(em.all_points_by_id)


class TestFetchBulkDataDetectChangesGatesReindexBlock(unittest.TestCase):
    """The `if not detect_changes:` block that (re-)indexes points as
    static must be skipped entirely when detect_changes=True — an inverted
    condition (`if detect_changes:`) would instead re-index already-known
    points on every normal detect_changes=True poll."""

    def _resp(self, point_id):
        return {
            str(point_id): {
                'title': 'Existing', 'description': '',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                             'isWritable': False},
                'value': {'integerValue': 1, 'stringValue': '', 'isOk': True},
            }
        }

    def test_already_baseline_point_not_reindexed_when_detect_changes_true(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em.baseline_point_ids.add(960)
        em.published_configs.add(960)  # already fully known -> skips the first block too
        em._api.fetch_bulk_points.return_value = self._resp(960)
        with patch.object(em, '_index_point') as mock_index:
            em._fetch_bulk_data(detect_changes=True)
        mock_index.assert_not_called()


class TestFetchBulkDataReindexPathFieldCorrectness(unittest.TestCase):
    """The `if not detect_changes:` re-indexing block builds its own
    display_title/description dict independently of the genuinely-new-
    point block above it — verified separately since mutmut generates
    distinct key-typo mutants for each occurrence."""

    def test_reindex_path_sets_display_title_and_description_from_response(self):
        em = _make_em()
        em._api.fetch_bulk_points.return_value = {
            '961': {
                'title': 'ReindexTitle', 'description': 'ReindexDesc',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                             'isWritable': False},
                'value': {'integerValue': 1, 'stringValue': '', 'isOk': True},
            }
        }
        em._fetch_bulk_data(detect_changes=False)
        self.assertEqual(em.all_points_by_id[961]['display_title'], 'ReindexTitle')
        self.assertEqual(em.all_points_by_id[961]['description'], 'ReindexDesc')


class TestFetchBulkDataLockBusy(unittest.TestCase):
    """_fetch_bulk_data returns False and logs when lock is already held."""

    def test_returns_false_when_lock_busy(self):
        em = _make_em()
        em._bulk_fetch_lock.acquire()
        try:
            result = em._fetch_bulk_data()
        finally:
            em._bulk_fetch_lock.release()
        self.assertIs(result, False)


class TestFetchBulkDataBadResponse(unittest.TestCase):
    """_fetch_bulk_data handles non-dict API response."""

    def test_none_response_calls_handle_api_failure(self):
        em = _make_em()
        em._api.fetch_bulk_points.return_value = None
        with patch.object(em, '_handle_api_failure') as mock_fail:
            result = em._fetch_bulk_data()
        self.assertIs(result, False)
        mock_fail.assert_called_once()

    def test_list_response_calls_handle_api_failure(self):
        em = _make_em()
        em._api.fetch_bulk_points.return_value = []
        with patch.object(em, '_handle_api_failure') as mock_fail:
            result = em._fetch_bulk_data()
        self.assertIs(result, False)
        mock_fail.assert_called_once()


class TestFetchBulkDataHttpErrors(unittest.TestCase):
    """_fetch_bulk_data handles HTTPError and generic exceptions."""

    def test_http_401_calls_handle_api_failure(self):
        import urllib.error
        em = _make_em()
        err = urllib.error.HTTPError(url='', code=401, msg='Unauthorized',
                                     hdrs=None, fp=None)
        em._api.fetch_bulk_points.side_effect = err
        with patch.object(em, '_handle_api_failure') as mock_fail:
            result = em._fetch_bulk_data()
        self.assertIs(result, False)
        mock_fail.assert_called_once()

    def test_http_401_passes_captured_last_error_to_handle_api_failure(self):
        """Regression: _fetch_bulk_data must capture self._api.last_error
        immediately in the except block and pass it explicitly to
        _handle_api_failure — not rely on _handle_api_failure re-reading
        self._api.last_error later, which races against the write-command
        executor thread's own concurrent request()/last_error writes."""
        import urllib.error
        em = _make_em()
        err = urllib.error.HTTPError(url='', code=401, msg='Unauthorized',
                                     hdrs=None, fp=None)
        em._api.fetch_bulk_points.side_effect = err
        em._api.last_error = "HTTP 401 — authentication rejected, check credentials"
        with patch.object(em, '_handle_api_failure') as mock_fail:
            em._fetch_bulk_data()
        mock_fail.assert_called_once_with(
            "HTTP 401 — authentication rejected, check credentials"
        )

    def test_http_503_calls_handle_api_failure(self):
        import urllib.error
        em = _make_em()
        err = urllib.error.HTTPError(url='', code=503, msg='Service Unavailable',
                                     hdrs=None, fp=None)
        em._api.fetch_bulk_points.side_effect = err
        with patch.object(em, '_handle_api_failure') as mock_fail:
            result = em._fetch_bulk_data()
        self.assertIs(result, False)
        mock_fail.assert_called_once()

    def test_unhandled_exception_calls_handle_api_failure(self):
        em = _make_em()
        em._api.fetch_bulk_points.side_effect = RuntimeError("unexpected")
        with patch.object(em, '_handle_api_failure') as mock_fail:
            result = em._fetch_bulk_data()
        self.assertIs(result, False)
        mock_fail.assert_called_once()


class TestFetchBulkDataValueError(unittest.TestCase):
    """ValueError/KeyError during point processing is logged and skipped."""

    def test_value_error_during_processing_continues(self):
        em = _make_em()
        em.initial_discovery_complete = True
        # A point whose metadata causes a TypeError when processed
        em._api.fetch_bulk_points.return_value = {
            'bad': {   # non-integer key → ValueError in int(point_id_str)
                'title': 'Bad', 'description': '',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                              'isWritable': False},
                'value': {'integerValue': 0, 'stringValue': '', 'isOk': True},
            },
            '100': {
                'title': 'Good', 'description': '',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                              'isWritable': False},
                'value': {'integerValue': 42, 'stringValue': '', 'isOk': True},
            },
        }
        em._fetch_bulk_data(detect_changes=False)
        # Should not raise; good point should be processed
        self.assertIn(100, em.bulk_data)


class TestFetchBulkDataPostWriteNoChanges(unittest.TestCase):
    """Post-write scan with no dynamic changes logs debug message."""

    def test_post_write_no_changes_debug_log(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em.post_write_active = True
        em._api.fetch_bulk_points.return_value = {
            '100': {
                'title': 'T', 'description': '',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                              'isWritable': False},
                'value': {'integerValue': 1, 'stringValue': '', 'isOk': True},
            }
        }
        em.baseline_point_ids.add(100)
        with patch.object(em, '_publish_dynamic_changes') as mock_dyn:
            em._fetch_bulk_data(detect_changes=True)
        mock_dyn.assert_not_called()


class TestFetchBulkDataApiRestoration(unittest.TestCase):
    """Successful fetch after failures dismisses api_unreachable notification."""

    def _minimal_response(self, point_id=100):
        """Non-empty truthy response that skips new-point routing."""
        return {
            str(point_id): {
                'title': 'T', 'description': '',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                              'isWritable': False},
                'value': {'integerValue': 1, 'stringValue': '', 'isOk': True},
            }
        }

    def test_dismisses_notification_and_publishes_alert_after_failures(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em.baseline_point_ids.add(100)
        em.api_consecutive_failures = em.api_failure_threshold + 1
        em._api_notification_active = True
        em._api.fetch_bulk_points.return_value = self._minimal_response()
        em._fetch_bulk_data(detect_changes=False)
        em._dismiss.assert_called()
        self.assertFalse(em._api_notification_active)

    def test_dismisses_discovery_notification_after_failures(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em.baseline_point_ids.add(100)
        em._discovery_notification_active = True
        em._api.fetch_bulk_points.return_value = self._minimal_response()
        em._fetch_bulk_data(detect_changes=False)
        self.assertFalse(em._discovery_notification_active)

    def test_failure_count_exactly_at_threshold_triggers_dismiss(self):
        """The recovery check is `api_consecutive_failures >= api_failure_threshold`
        (>=, not >). At exact equality with the threshold, the dismiss/alert
        block must still fire — a `>=`->`>` mutation would require one more
        failure than actually occurred before ever recovering."""
        em = _make_em()
        em.initial_discovery_complete = True
        em.baseline_point_ids.add(100)
        em.api_consecutive_failures = em.api_failure_threshold  # exact boundary
        em._api_notification_active = True
        em._api.fetch_bulk_points.return_value = self._minimal_response()
        em._fetch_bulk_data(detect_changes=False)
        em._dismiss.assert_called()
        self.assertFalse(em._api_notification_active)


class TestFetchBulkDataDetectChangesDefault(unittest.TestCase):
    """_fetch_bulk_data's detect_changes parameter defaults to True. Calling
    with no argument, on a known dynamic point appearing outside baseline/
    published_configs, must route it through the new-points/dynamic-change
    path exactly as an explicit detect_changes=True call would — a mutated
    default of False would instead route it as index_point-static (no dynamic
    routing) and never call _publish_dynamic_changes."""

    def test_default_call_detects_dynamic_point_changes(self):
        from nibe_dynamic_map import DynamicPointEntry
        em = _make_em()
        em.initial_discovery_complete = True
        em.dynamic_point_map._table[77] = DynamicPointEntry(
            point_id=77, title='Switch', entity_type='switch',
            processed_values={0, 1}, is_controlling=True,
            dynamic_points_by_value={1: [700]},
        )
        em._api.fetch_bulk_points.return_value = {
            '700': {
                'title': 'T', 'description': '',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                             'isWritable': False},
                'value': {'integerValue': 1, 'stringValue': '', 'isOk': True},
            }
        }
        with patch.object(em, '_publish_dynamic_changes') as mock_dyn:
            result = em._fetch_bulk_data()  # no detect_changes arg
        mock_dyn.assert_called_once()
        self.assertIs(result, True)


class TestFetchBulkDataDisappearedGuardRequiresDetectChanges(unittest.TestCase):
    """The disappeared-points computation block is gated by
    `if detect_changes and self.initial_discovery_complete:` (AND). When
    detect_changes=False (e.g. the initial-discovery poll) but
    initial_discovery_complete happens to already be True, an `and`->`or`
    mutation would let this block run anyway — including the post-write
    'newly absent' branch that mutates baseline_point_ids as a side effect,
    even though the caller explicitly asked for no change detection."""

    def test_baseline_point_ids_untouched_when_detect_changes_false(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em.post_write_active = True
        em._post_write_until = time.time() + 9999
        em.baseline_point_ids.add(555)  # absent from this fetch's response
        em.baseline_point_ids.add(999)  # present, stays
        em._api.fetch_bulk_points.return_value = {
            '999': {
                'title': 'T', 'description': '',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                             'isWritable': False},
                'value': {'integerValue': 1, 'stringValue': '', 'isOk': True},
            }
        }
        with patch.object(em, '_publish_dynamic_changes') as mock_dyn:
            em._fetch_bulk_data(detect_changes=False)
        mock_dyn.assert_not_called()
        self.assertIn(555, em.baseline_point_ids,
            "baseline_point_ids must not be mutated by the disappeared-points "
            "logic when detect_changes=False")


class TestFetchBulkDataNewSwitchPopulatesMap(unittest.TestCase):
    """New switch/select points discovered outside scan call populate_from_bulk."""

    def test_new_switch_point_calls_populate_from_bulk(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em._api.fetch_bulk_points.return_value = {
            '500': {
                'title': 'Mode switch', 'description': '',
                'metadata': {
                    'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                    'isWritable': True, 'minValue': 0, 'maxValue': 1,
                    'variableType': 'integer', 'variableSize': 'u8',
                    'divisor': 1, 'decimal': 0, 'unit': '',
                },
                'value': {'integerValue': 0, 'stringValue': '', 'isOk': True},
            }
        }
        with patch.object(em.dynamic_point_map, 'populate_from_bulk') as mock_pop:
            em._fetch_bulk_data(detect_changes=True)
        mock_pop.assert_called_once()


class TestUpdateAllStates(unittest.TestCase):
    """update_all_states: force flag, post-write window, active entity loop."""

    def _point_response(self, point_id=100, value=1):
        return {
            str(point_id): {
                'title': 'Test', 'description': '',
                'metadata': {
                    'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
                    'isWritable': False, 'variableType': 'integer',
                    'variableSize': 'u8', 'divisor': 1, 'decimal': 0, 'unit': '',
                },
                'value': {'integerValue': value, 'stringValue': '', 'isOk': True},
            }
        }

    def test_no_active_entities_returns_without_iterating(self):
        em = _make_em()
        em._api.fetch_bulk_points.return_value = {}
        with patch.object(em, '_update_entity_state') as mock_update:
            em.update_all_states()
        mock_update.assert_not_called()

    def test_active_entities_calls_update_entity_state(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em._api.fetch_bulk_points.return_value = self._point_response(100, 1)
        entity_info = {
            'point_id': 100, 'entity_type': 'sensor',
            'availability_topic': 'nibe/avail/100',
            'state_topic': 'nibe/state/100',
            'command_topic': None, 'point_data': {},
        }
        em.active_entities_by_id[100] = entity_info
        with patch.object(em, '_update_entity_state') as mock_update:
            em.update_all_states()
        mock_update.assert_called_once_with(entity_info)

    def test_force_resets_last_bulk_fetch(self):
        em = _make_em()
        em.last_bulk_fetch = 99999.0
        em._api.fetch_bulk_points.return_value = {}
        em.update_all_states(force=True)
        # last_bulk_fetch is reset to 0 then updated to current time — either way != 99999
        self.assertNotEqual(em.last_bulk_fetch, 99999.0)

    def test_post_write_window_ends_when_expired(self):
        em = _make_em()
        em.post_write_active = True
        em._post_write_until = 0.0   # already past
        em._api.fetch_bulk_points.return_value = {}
        em.update_all_states()
        self.assertFalse(em.post_write_active)

    def test_post_write_window_stays_active_when_not_yet_expired(self):
        """The end condition is `post_write_active AND expired`, not OR —
        a still-active, not-yet-expired window must NOT end early. An
        `and`->`or` mutation here would end the fast-polling window on
        nearly every call regardless of whether it actually expired,
        since post_write_active is independently True."""
        em = _make_em()
        em.post_write_active = True
        em._post_write_until = time.time() + 9999  # far in the future
        em._api.fetch_bulk_points.return_value = {}
        em.update_all_states()
        self.assertTrue(em.post_write_active)


class TestUpdateAllStatesActiveEntitiesLogging(unittest.TestCase):
    """update_all_states must log the active-entity count with the correct
    format string AND the count as a positional arg — not the count passed
    as the lone positional (would break %-formatting of the message) and not
    the format string with no arg at all (would silently drop the count)."""

    def test_debug_log_called_with_format_string_and_count_arg(self):
        em = _make_em()
        em._api.fetch_bulk_points.return_value = {}
        em.active_entities_by_id[100] = {
            'point_id': 100, 'entity_type': 'sensor',
            'availability_topic': 'a', 'state_topic': 's',
            'command_topic': None, 'point_data': {},
        }
        em.active_entities_by_id[101] = {
            'point_id': 101, 'entity_type': 'sensor',
            'availability_topic': 'a2', 'state_topic': 's2',
            'command_topic': None, 'point_data': {},
        }
        with patch('nibe_entity_manager.log_entities.debug') as mock_debug, \
             patch.object(em, '_update_entity_state'):
            em.update_all_states()
        mock_debug.assert_called_once_with("Updating %d active entities", 2)


class TestFetchBulkDataMetadataUpdate(unittest.TestCase):
    """_fetch_bulk_data must use equality (!=) not identity (is not) when
    deciding whether to update bulk_data['metadata'].  Since every API
    response produces a fresh dict object, identity comparison always
    returns True, meaning metadata was reassigned on every poll regardless
    of whether the content changed.  This test locks in the correct behaviour."""

    def _response(self, point_id, meta_override=None):
        meta = {
            'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
            'isWritable': False, 'variableType': 'integer',
            'variableSize': 'u8', 'divisor': 1, 'minValue': 0, 'maxValue': 100,
        }
        if meta_override:
            meta.update(meta_override)
        return {
            str(point_id): {
                'title': 'Test', 'description': '',
                'metadata': meta,
                'value': {'integerValue': 42, 'stringValue': '', 'isOk': True},
            }
        }

    def test_metadata_not_reassigned_when_content_unchanged(self):
        """When API returns metadata with the same content as before, the
        existing bulk_data['metadata'] object must NOT be replaced — the
        equality check must prevent unnecessary reassignment."""
        em = _make_em()
        em.initial_discovery_complete = True
        em.baseline_point_ids.add(100)

        # First fetch — populates bulk_data
        em._api.fetch_bulk_points.return_value = self._response(100)
        em._fetch_bulk_data(detect_changes=False)
        original_metadata = em.bulk_data[100]['metadata']

        # Second fetch — same metadata content, new dict object
        em._api.fetch_bulk_points.return_value = self._response(100)
        em._fetch_bulk_data(detect_changes=False)

        # Metadata object must be the SAME as after the first fetch
        # (not replaced by a new dict with identical content)
        self.assertIs(em.bulk_data[100]['metadata'], original_metadata,
            "Metadata must not be reassigned when content is unchanged — "
            "use != not 'is not' for the comparison")

    def test_metadata_updated_when_content_changes(self):
        """When API returns metadata with different content, bulk_data['metadata']
        must be updated to reflect the change."""
        em = _make_em()
        em.initial_discovery_complete = True
        em.baseline_point_ids.add(100)

        em._api.fetch_bulk_points.return_value = self._response(100, {'divisor': 1})
        em._fetch_bulk_data(detect_changes=False)

        em._api.fetch_bulk_points.return_value = self._response(100, {'divisor': 10})
        em._fetch_bulk_data(detect_changes=False)

        self.assertEqual(em.bulk_data[100]['metadata']['divisor'], 10)

    def test_implementation_uses_equality_not_identity(self):
        """Regression guard: verify the source uses != not 'is not'."""
        import inspect

        from nibe_entity_manager import EntityManager
        src = inspect.getsource(EntityManager._fetch_bulk_data)
        self.assertNotIn("is not metadata", src,
            "_fetch_bulk_data must use != not 'is not' for metadata comparison")
        self.assertIn("!= metadata", src)

    """_publish_dynamic_changes: ValueError/TypeError/AttributeError raised
    in the HA notification block must be caught and logged, not propagated
    (lines 1754-1757)."""

    def test_notification_exception_does_not_propagate(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em._post_write_controlling_point = None

        point_id = 500
        em.all_points_by_id[point_id] = {
            'variableId': point_id, 'display_title': 'Test', 'description': '',
            'entity_type': 'sensor', 'entity_category': 'diagnostic',
            'is_writable': False, 'is_dynamic': True,
            'metadata': {'isWritable': False, 'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                         'variableType': 'integer', 'variableSize': 'u8',
                         'divisor': 1, 'minValue': 0, 'maxValue': 1},
        }

        new_points = [(point_id, {
            'title': 'Test', 'description': '',
            'metadata': em.all_points_by_id[point_id]['metadata'],
            'value': {'integerValue': 0, 'stringValue': '', 'isOk': True},
        })]

        # Make _notify raise a ValueError — should be caught, not propagated
        em._notify.side_effect = ValueError("simulated notification error")

        # _publish_dynamic_changes acquires _em_lock and calls _enable_entity_locked directly
        with patch.object(em, '_enable_entity_locked'):
            try:
                em._publish_dynamic_changes(new_points, set())
            except (ValueError, TypeError, AttributeError) as e:
                self.fail(f"Exception should have been caught: {e}")


class TestFetchBulkDataMalformed(unittest.TestCase):
    """Test _fetch_bulk_data with malformed response entries."""

    def test_skip_non_dict_point(self):
        em = _make_em()
        em._api.fetch_bulk_points.return_value = {
            '100': {
                'title': 'Good point',
                'metadata': {},
                'value': {'integerValue': 0, 'isOk': True}
            },
            '200': ['not', 'a', 'dict'],
        }
        em._fetch_bulk_data(detect_changes=False)
        self.assertIn(100, em.bulk_data)
        self.assertNotIn(200, em.bulk_data)

    def test_point_without_value_key(self):
        em = _make_em()
        em._api.fetch_bulk_points.return_value = {
            '300': {
                'title': 'Missing value',
                'metadata': {},
            }
        }
        em._fetch_bulk_data(detect_changes=False)
        self.assertIn(300, em.bulk_data)
        self.assertEqual(em.bulk_data[300]['raw_value'], 0)
        self.assertFalse(em.bulk_data[300]['is_ok'])

    def test_point_with_explicit_null_value_and_metadata(self):
        """An API response with "value": null / "metadata": null (present but
        None, not merely absent) must not abort the whole bulk fetch — other
        points in the same response must still be processed."""
        em = _make_em()
        em._api.fetch_bulk_points.return_value = {
            '400': {
                'title': 'Null value point',
                'metadata': None,
                'value': None,
            },
            '401': {
                'title': 'Good point',
                'metadata': {},
                'value': {'integerValue': 42, 'isOk': True},
            },
        }
        em._fetch_bulk_data(detect_changes=False)
        self.assertIn(400, em.bulk_data)
        self.assertEqual(em.bulk_data[400]['raw_value'], 0)
        self.assertFalse(em.bulk_data[400]['is_ok'])
        self.assertIn(401, em.bulk_data)
        self.assertEqual(em.bulk_data[401]['raw_value'], 42)


class TestUpdateAllStatesIntervalAndLockBusy(unittest.TestCase):
    """update_all_states: interval-not-elapsed and lock-busy branches."""

    def test_fetch_skipped_when_interval_not_elapsed(self):
        """When interval has not elapsed, fetch_bulk_points must not be called."""
        em = _make_em()
        em.last_bulk_fetch = time.time()
        em.bulk_interval   = 99999
        em._api.fetch_bulk_points.return_value = {}
        em.update_all_states()
        em._api.fetch_bulk_points.assert_not_called()

    def test_last_bulk_fetch_not_updated_when_lock_busy(self):
        """When lock was busy (_fetch_bulk_data returns False, failures unchanged),
        last_bulk_fetch must not advance so the next call retries immediately."""
        em = _make_em()
        em.last_bulk_fetch = 0.0
        original_failures  = em.api_consecutive_failures
        with patch.object(em, '_fetch_bulk_data', return_value=False):
            em.update_all_states()
        self.assertEqual(em.api_consecutive_failures, original_failures)
        self.assertEqual(em.last_bulk_fetch, 0.0,
                         "last_bulk_fetch must not be updated when lock was busy")


class TestUpdateAllStatesDetailedBranches(unittest.TestCase):
    """update_all_states: fine-grained branch coverage for the post-write
    window boundary, the should_detect computation, and the lock-was-busy
    vs. genuine-failure distinction. Complements
    TestUpdateAllStatesIntervalAndLockBusy above."""

    def test_force_reset_to_zero_makes_interval_check_pass_at_boundary(self):
        """force=True must reset last_bulk_fetch to exactly 0, not some other
        falsy-looking value. Pin current_time to exactly bulk_interval so
        (current_time - 0) >= interval is true but (current_time - 1) would
        be false — this distinguishes 0 from 1 (both falsy is irrelevant;
        what matters is the arithmetic result)."""
        em = _make_em()
        em.bulk_interval = 30
        em.last_bulk_fetch = 12345.0  # stale value, must be overwritten by force
        with (
            patch('time.time', return_value=30.0),
            patch.object(em, '_fetch_bulk_data', return_value=True) as mock_fetch,
        ):
            em.update_all_states(force=True)
        mock_fetch.assert_called_once()

    def test_post_write_window_boundary_exact_equality_not_yet_expired(self):
        """The expiry check is `current_time > _post_write_until` (strict).
        At exact equality the window must NOT be considered expired yet."""
        em = _make_em()
        em.post_write_active = True
        em._post_write_until = 500.0
        em._api.fetch_bulk_points.return_value = {}
        with patch('time.time', return_value=500.0):
            em.update_all_states()
        self.assertIs(em.post_write_active, True)

    def test_post_write_active_set_to_false_not_none_on_expiry(self):
        """post_write_active must become the literal False, not None —
        assertFalse alone can't tell these apart, so use assertIs."""
        em = _make_em()
        em.post_write_active = True
        em._post_write_until = 0.0
        em._api.fetch_bulk_points.return_value = {}
        em.update_all_states()
        self.assertIs(em.post_write_active, False)

    def test_post_write_controlling_point_cleared_to_none_on_expiry(self):
        """_post_write_controlling_point must be reset to None (not ''),
        since downstream code checks `is None` to detect 'no controlling
        point snapshot'."""
        em = _make_em()
        em.post_write_active = True
        em._post_write_until = 0.0
        em._post_write_controlling_point = 42
        em._api.fetch_bulk_points.return_value = {}
        em.update_all_states()
        self.assertIsNone(em._post_write_controlling_point)

    def test_should_detect_true_when_known_dynamic_points_exist(self):
        """should_detect = bool(known_count) or post_write_active. With
        known dynamic points present and post-write inactive, should_detect
        must be exactly True — not None, not falsy-but-wrong."""
        from nibe_dynamic_map import DynamicPointEntry
        em = _make_em()
        em.post_write_active = False
        em.dynamic_point_map._table[77] = DynamicPointEntry(
            point_id=77, title='Test Switch', entity_type='switch',
            processed_values={0, 1}, is_controlling=True,
            dynamic_points_by_value={1: [22001, 22002]},
        )
        self.assertGreater(len(em.dynamic_point_map.all_known_dynamic_point_ids()), 0)
        with patch.object(em, '_fetch_bulk_data', return_value=True) as mock_fetch:
            em.update_all_states()
        mock_fetch.assert_called_once_with(detect_changes=True)

    def test_should_detect_false_when_no_known_points_and_not_post_write(self):
        """With zero known dynamic points and post-write inactive,
        should_detect must be exactly False (bool(0) or False == False) —
        catches `or`->`and` and `bool(known_count)`->`bool(None)` mutants,
        which would each independently still yield a falsy-but-wrong or
        wrong-typed value in other scenarios, but here the correct answer
        is unambiguously False."""
        em = _make_em()
        em.post_write_active = False
        self.assertEqual(len(em.dynamic_point_map.all_known_dynamic_point_ids()), 0)
        with patch.object(em, '_fetch_bulk_data', return_value=True) as mock_fetch:
            em.update_all_states()
        mock_fetch.assert_called_once_with(detect_changes=False)

    def test_should_detect_true_via_post_write_even_with_no_known_points(self):
        """or (not and): post_write_active=True alone, with zero known
        dynamic points, must still make should_detect True. An `or`->`and`
        mutation would instead force it to False here."""
        em = _make_em()
        em.post_write_active = True
        em._post_write_until = time.time() + 9999
        self.assertEqual(len(em.dynamic_point_map.all_known_dynamic_point_ids()), 0)
        with patch.object(em, '_fetch_bulk_data', return_value=True) as mock_fetch:
            em.update_all_states()
        mock_fetch.assert_called_once_with(detect_changes=True)

    def test_lock_was_busy_uses_and_not_or_on_success_with_no_failure_change(self):
        """lock_was_busy = (result is False) AND (failures unchanged). When
        _fetch_bulk_data succeeds (result=True, e.g. a genuine change was
        detected) and failures happen to be unchanged, lock_was_busy must
        be False (since result is not False) — so last_bulk_fetch DOES
        advance. An `and`->`or` mutation would wrongly make the second
        clause alone (failures unchanged) sufficient to call it 'lock busy',
        blocking last_bulk_fetch from advancing even on real success."""
        em = _make_em()
        em.last_bulk_fetch = 0.0
        with (
            patch('time.time', return_value=999.0),
            patch.object(em, '_fetch_bulk_data', return_value=True),
        ):
            em.update_all_states()
        self.assertEqual(em.last_bulk_fetch, 999.0)

    def test_last_bulk_fetch_set_to_current_time_value_not_placeholder(self):
        """last_bulk_fetch must be set to the actual current_time value
        captured at the top of the function, not merely truthy — pin
        time.time() to a distinctive, independently-known value and check
        it's stored verbatim."""
        em = _make_em()
        em.last_bulk_fetch = 0.0
        with (
            patch('time.time', return_value=54321.5),
            patch.object(em, '_fetch_bulk_data', return_value=True),
        ):
            em.update_all_states()
        self.assertEqual(em.last_bulk_fetch, 54321.5)


class TestFetchBulkDataApiRestorationNoPriorNotification(unittest.TestCase):
    """_fetch_bulk_data: 1564→1584 — api_consecutive_failures >= threshold
    but _api_notification_active is False (notification was never sent).

    Steady-state normal recovery: the bridge always succeeds so no notification
    was ever raised.  The dismiss/alert block must be skipped entirely.
    """

    def _minimal_response(self, point_id=100):
        return {
            str(point_id): {
                'title': 'T', 'description': '',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                             'isWritable': False},
                'value': {'integerValue': 1, 'stringValue': '', 'isOk': True},
            }
        }

    def test_no_prior_notification_skips_dismiss_and_alert(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em.baseline_point_ids.add(100)
        em.api_consecutive_failures = em.api_failure_threshold + 1
        em._api_notification_active = False   # notification was never raised
        em._api.fetch_bulk_points.return_value = self._minimal_response()
        em._fetch_bulk_data(detect_changes=False)
        # dismiss must NOT have been called (no notification to dismiss)
        em._dismiss.assert_not_called()

    def test_pub_none_skips_bridge_alert_on_recovery(self):
        """1570→1584: _api_notification_active=True but _pub is None —
        dismiss fires but publish_bridge_alert is never called."""
        em = _make_em()
        em.initial_discovery_complete = True
        em.baseline_point_ids.add(100)
        em.api_consecutive_failures = em.api_failure_threshold + 1
        em._api_notification_active = True
        em._pub = None   # publisher not yet wired (edge case at startup)
        em._api.fetch_bulk_points.return_value = self._minimal_response()
        # Must not raise AttributeError accessing _pub.publish_bridge_alert
        em._fetch_bulk_data(detect_changes=False)
        self.assertFalse(em._api_notification_active)


class TestFetchBulkDataDisappearedPoints(unittest.TestCase):
    """_fetch_bulk_data: disappeared_points computation — the intersection
    of known dynamic points that were previously active but are absent
    from this fetch's current_point_ids."""

    def _resp(self, point_id):
        return {
            str(point_id): {
                'title': 'T', 'description': '',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                             'isWritable': False},
                'value': {'integerValue': 1, 'stringValue': '', 'isOk': True},
            }
        }

    def _setup_known_dynamic(self, em, controlling_id=77, dynamic_id=22001):
        from nibe_dynamic_map import DynamicPointEntry
        em.dynamic_point_map._table[controlling_id] = DynamicPointEntry(
            point_id=controlling_id, title='Switch', entity_type='switch',
            processed_values={0, 1}, is_controlling=True,
            dynamic_points_by_value={1: [dynamic_id]},
        )

    def test_active_known_dynamic_point_missing_from_response_is_disappeared(self):
        """A point that is both a known dynamic point AND currently active,
        but absent from this fetch's response, must be reported to
        _publish_dynamic_changes as disappeared."""
        em = _make_em()
        em.initial_discovery_complete = True
        self._setup_known_dynamic(em, controlling_id=77, dynamic_id=22001)
        em.active_dynamic_points.add(22001)
        em.baseline_point_ids.add(999)  # unrelated point present in response
        em._api.fetch_bulk_points.return_value = self._resp(999)  # 22001 absent
        with patch.object(em, '_publish_dynamic_changes') as mock_dyn:
            em._fetch_bulk_data(detect_changes=True)
        mock_dyn.assert_called_once()
        _, kwargs_or_args = mock_dyn.call_args[0][0], mock_dyn.call_args[0][1]
        self.assertIn(22001, kwargs_or_args)

    def test_known_dynamic_point_not_active_and_missing_is_not_disappeared(self):
        """A known dynamic point that is NOT in active_dynamic_points but is
        absent must NOT be reported as disappeared — only currently-active
        dynamic points can 'disappear'. Distinguishes the `&` (intersection)
        from a plain membership check."""
        em = _make_em()
        em.initial_discovery_complete = True
        self._setup_known_dynamic(em, controlling_id=77, dynamic_id=22001)
        # 22001 is known-dynamic but NOT currently active
        em.baseline_point_ids.add(999)
        em._api.fetch_bulk_points.return_value = self._resp(999)
        with patch.object(em, '_publish_dynamic_changes') as mock_dyn:
            em._fetch_bulk_data(detect_changes=True)
        mock_dyn.assert_not_called()

    def test_active_dynamic_point_present_in_response_is_not_disappeared(self):
        """A currently-active known dynamic point that IS present in this
        fetch's response must not be treated as disappeared."""
        em = _make_em()
        em.initial_discovery_complete = True
        self._setup_known_dynamic(em, controlling_id=77, dynamic_id=22001)
        em.active_dynamic_points.add(22001)
        em.published_configs = {22001}  # already published — not a "new" appearance
        em._api.fetch_bulk_points.return_value = self._resp(22001)
        with patch.object(em, '_publish_dynamic_changes') as mock_dyn:
            em._fetch_bulk_data(detect_changes=True)
        mock_dyn.assert_not_called()


class TestFetchBulkDataPostWriteNewlyAbsent(unittest.TestCase):
    """_fetch_bulk_data: during a post-write scan, a baseline point that
    goes absent (and is not already a known dynamic point) is treated as a
    newly-discovered dynamic disappearance — removed from baseline_point_ids
    and added to disappeared_points."""

    def _resp(self, point_id):
        return {
            str(point_id): {
                'title': 'T', 'description': '',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                             'isWritable': False},
                'value': {'integerValue': 1, 'stringValue': '', 'isOk': True},
            }
        }

    def test_baseline_point_absent_during_post_write_becomes_disappeared(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em.post_write_active = True
        em._post_write_until = time.time() + 9999
        em.baseline_point_ids.add(555)  # will go missing this fetch
        em.baseline_point_ids.add(999)  # present, stays
        em._api.fetch_bulk_points.return_value = self._resp(999)  # 555 absent
        with patch.object(em, '_publish_dynamic_changes') as mock_dyn:
            em._fetch_bulk_data(detect_changes=True)
        mock_dyn.assert_called_once()
        disappeared_arg = mock_dyn.call_args[0][1]
        self.assertIn(555, disappeared_arg)
        self.assertNotIn(555, em.baseline_point_ids,
            "point must be discarded from baseline_point_ids once reclassified as dynamic")

    def test_baseline_point_absent_outside_post_write_is_not_reclassified(self):
        """The same absence, but OUTSIDE a post-write scan window, must NOT
        trigger the newly-absent reclassification — baseline_point_ids must
        be left untouched and no dynamic-change event fired."""
        em = _make_em()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em.baseline_point_ids.add(555)
        em.baseline_point_ids.add(999)
        em._api.fetch_bulk_points.return_value = self._resp(999)
        with patch.object(em, '_publish_dynamic_changes') as mock_dyn:
            em._fetch_bulk_data(detect_changes=True)
        mock_dyn.assert_not_called()
        self.assertIn(555, em.baseline_point_ids)


class TestFetchBulkDataReturnValue(unittest.TestCase):
    """_fetch_bulk_data's return value contract:
      detect_changes=True  -> bool(new_points or disappeared_points)
      detect_changes=False -> True (unconditional success)
    """

    def _resp(self, point_id):
        return {
            str(point_id): {
                'title': 'T', 'description': '',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                             'isWritable': False},
                'value': {'integerValue': 1, 'stringValue': '', 'isOk': True},
            }
        }

    def test_detect_changes_true_no_changes_returns_false(self):
        """With detect_changes=True and nothing new/disappeared, the return
        value must be exactly False, not True and not None."""
        em = _make_em()
        em.initial_discovery_complete = True
        em.baseline_point_ids.add(100)
        em._api.fetch_bulk_points.return_value = self._resp(100)
        result = em._fetch_bulk_data(detect_changes=True)
        self.assertIs(result, False)

    def test_detect_changes_true_with_new_point_returns_true(self):
        """A known dynamic point appearing outside baseline/published_configs
        is routed into new_points (see _fetch_bulk_data's dynamic-appearance
        branch), so the return value must be exactly True."""
        from nibe_dynamic_map import DynamicPointEntry
        em = _make_em()
        em.initial_discovery_complete = True
        em.dynamic_point_map._table[77] = DynamicPointEntry(
            point_id=77, title='Switch', entity_type='switch',
            processed_values={0, 1}, is_controlling=True,
            dynamic_points_by_value={1: [700]},
        )
        # baseline_point_ids empty, published_configs empty -> point 700 counts as new
        em._api.fetch_bulk_points.return_value = self._resp(700)
        result = em._fetch_bulk_data(detect_changes=True)
        self.assertIs(result, True)

    def test_detect_changes_false_always_returns_true(self):
        """detect_changes=False skips the change-detection ternary entirely
        and must return exactly True regardless of what happened during
        this fetch (even a point that would otherwise look 'new')."""
        em = _make_em()
        em.initial_discovery_complete = True
        em._api.fetch_bulk_points.return_value = self._resp(700)
        result = em._fetch_bulk_data(detect_changes=False)
        self.assertIs(result, True)


class TestFetchBulkDataBookkeeping(unittest.TestCase):
    """_fetch_bulk_data's end-of-cycle bookkeeping: published_configs,
    last_fetch_duration, api_consecutive_failures reset, api_last_success_time."""

    def _resp(self, point_id):
        return {
            str(point_id): {
                'title': 'T', 'description': '',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                             'isWritable': False},
                'value': {'integerValue': 1, 'stringValue': '', 'isOk': True},
            }
        }

    def test_published_configs_set_to_current_point_ids(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em.baseline_point_ids.add(100)
        em._api.fetch_bulk_points.return_value = self._resp(100)
        em._fetch_bulk_data(detect_changes=False)
        self.assertEqual(em.published_configs, {100})

    def test_api_consecutive_failures_reset_to_zero_on_success(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em.baseline_point_ids.add(100)
        em.api_consecutive_failures = 7
        em._api.fetch_bulk_points.return_value = self._resp(100)
        em._fetch_bulk_data(detect_changes=False)
        self.assertEqual(em.api_consecutive_failures, 0)

    def test_api_last_success_time_updated_on_success(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em.baseline_point_ids.add(100)
        em.api_last_success_time = 1.0
        em._api.fetch_bulk_points.return_value = self._resp(100)
        with patch('nibe_entity_manager.time.time', return_value=88888.0):
            em._fetch_bulk_data(detect_changes=False)
        self.assertEqual(em.api_last_success_time, 88888.0)

    def test_last_fetch_duration_reflects_elapsed_time(self):
        """last_fetch_duration must be computed from the actual elapsed
        wall-clock time (end - start), not merely set to some placeholder.
        Feed two distinct time.time() values (start, end) via side_effect
        and check the stored duration equals their difference exactly."""
        em = _make_em()
        em.initial_discovery_complete = True
        em.baseline_point_ids.add(100)
        em._api.fetch_bulk_points.return_value = self._resp(100)
        # time.time() is called: start_time, now (per-point loop uses one
        # more call), elapsed calc, api_last_success_time. Use a generous
        # side_effect sequence anchored on a known start/end gap.
        times = iter([1000.0] + [1007.5] * 30)
        with patch('nibe_entity_manager.time.time', side_effect=lambda: next(times)):
            em._fetch_bulk_data(detect_changes=False)
        self.assertEqual(em.last_fetch_duration, 7.5)


class TestFetchBulkDataControllingPointSnapshot(unittest.TestCase):
    """_fetch_bulk_data snapshots _post_write_controlling_point under the
    lock at the start of the cycle and passes that exact snapshot (not the
    live, possibly-since-mutated attribute) as the third positional arg to
    _publish_dynamic_changes."""

    def _resp(self, point_id):
        return {
            str(point_id): {
                'title': 'T', 'description': '',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                             'isWritable': False},
                'value': {'integerValue': 1, 'stringValue': '', 'isOk': True},
            }
        }

    def test_snapshot_value_passed_through_to_publish_dynamic_changes(self):
        from nibe_dynamic_map import DynamicPointEntry
        em = _make_em()
        em.initial_discovery_complete = True
        em.post_write_active = True
        em._post_write_until = time.time() + 9999
        em._post_write_controlling_point = 314159  # distinctive, independently-known value
        em.dynamic_point_map._table[77] = DynamicPointEntry(
            point_id=77, title='Switch', entity_type='switch',
            processed_values={0, 1}, is_controlling=True,
            dynamic_points_by_value={1: [700]},
        )
        em._api.fetch_bulk_points.return_value = self._resp(700)
        with patch.object(em, '_publish_dynamic_changes') as mock_dyn:
            em._fetch_bulk_data(detect_changes=True)
        mock_dyn.assert_called_once()
        self.assertEqual(mock_dyn.call_args[0][2], 314159)


class TestFetchBulkDataNewPermanentPointIndexing(unittest.TestCase):
    """_fetch_bulk_data: a genuinely unknown point (not in baseline, not
    published, not a known dynamic point, outside any post-write scan) is
    indexed as a new *static* point — verifies the dict fields passed to
    _index_point and that baseline_point_ids/publish calls fire."""

    def _resp(self, point_id, writable=True):
        return {
            str(point_id): {
                'title': 'New Sensor', 'description': 'desc',
                'metadata': {
                    'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                    'isWritable': writable,
                },
                'value': {'integerValue': 5, 'stringValue': '', 'isOk': True},
            }
        }

    def test_new_permanent_point_indexed_with_is_dynamic_false(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em._api.fetch_bulk_points.return_value = self._resp(600)
        em._fetch_bulk_data(detect_changes=True)
        self.assertIn(600, em.all_points_by_id)
        self.assertIs(em.all_points_by_id[600]['is_dynamic'], False)

    def test_new_permanent_point_is_writable_taken_from_metadata(self):
        """is_writable in the indexed point must reflect metadata['isWritable']
        (default False when absent) — not hardcoded."""
        em = _make_em()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em._api.fetch_bulk_points.return_value = self._resp(601, writable=True)
        em._fetch_bulk_data(detect_changes=True)
        self.assertIs(em.all_points_by_id[601]['is_writable'], True)

    def test_new_permanent_point_added_to_baseline_point_ids(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em._api.fetch_bulk_points.return_value = self._resp(602)
        em._fetch_bulk_data(detect_changes=True)
        self.assertIn(602, em.baseline_point_ids)

    def test_new_permanent_point_publishes_metadata_and_point_list(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em._api.fetch_bulk_points.return_value = self._resp(603)
        em._fetch_bulk_data(detect_changes=True)
        em._pub.publish_point_metadata.assert_called_once()
        em._pub.publish_point_list.assert_called_once()

    def test_new_permanent_point_display_title_and_description_from_response(self):
        em = _make_em()
        em.initial_discovery_complete = True
        em.post_write_active = False
        em._api.fetch_bulk_points.return_value = self._resp(604)
        em._fetch_bulk_data(detect_changes=True)
        self.assertEqual(em.all_points_by_id[604]['display_title'], 'New Sensor')
        self.assertEqual(em.all_points_by_id[604]['description'], 'desc')


class TestFetchBulkDataNoDetectChangesReindexing(unittest.TestCase):
    """_fetch_bulk_data: when detect_changes=False (e.g. initial discovery
    poll), every point NOT already a known dynamic point is (re-)indexed as
    static via the separate `if not detect_changes:` block — independent of
    the detect_changes-gated new/known-dynamic branch above it."""

    def _resp(self, point_id):
        return {
            str(point_id): {
                'title': 'Discovery Point', 'description': '',
                'metadata': {'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                             'isWritable': False},
                'value': {'integerValue': 9, 'stringValue': '', 'isOk': True},
            }
        }

    def test_point_indexed_when_detect_changes_false(self):
        em = _make_em()
        em._api.fetch_bulk_points.return_value = self._resp(700)
        em._fetch_bulk_data(detect_changes=False)
        self.assertIn(700, em.all_points_by_id)
        self.assertIs(em.all_points_by_id[700]['is_dynamic'], False)

    def test_known_dynamic_point_not_reindexed_when_detect_changes_false(self):
        """A point that IS a known dynamic point must be skipped by this
        block — it must not be (re-)indexed as static here."""
        from nibe_dynamic_map import DynamicPointEntry
        em = _make_em()
        em.dynamic_point_map._table[77] = DynamicPointEntry(
            point_id=77, title='Switch', entity_type='switch',
            processed_values={0, 1}, is_controlling=True,
            dynamic_points_by_value={1: [701]},
        )
        em._api.fetch_bulk_points.return_value = self._resp(701)
        em._fetch_bulk_data(detect_changes=False)
        self.assertNotIn(701, em.all_points_by_id)


class TestRawApiValueToFinalStatePayload(unittest.TestCase):
    """Raw Nibe API bulk response -> update_all_states() -> real
    _fetch_bulk_data() -> real _update_entity_state() -> real
    _process_and_publish_state() -> final MQTT state topic payload.
    This is the "read" counterpart of the discovery-config full-chain
    check in test_entity_manager_discovery.py's
    TestRawApiResponseToFinalDiscoveryPayload — that one chained a raw
    API response through classification into a discovery CONFIG; this
    chains a raw API response through the same _fetch_bulk_data() into the
    actual current VALUE published as an entity's state. Every existing
    _fetch_bulk_data test in this file mocks _update_entity_state out
    entirely, so no existing test lets a real raw API value reach a real
    final state payload."""

    def _raw_api_response(self, point_id, raw_int_value, divisor):
        return {
            str(point_id): {
                'title': 'Outdoor Temperature', 'description': '',
                'value': {'integerValue': raw_int_value, 'stringValue': '', 'isOk': True},
                'metadata': {
                    'modbusRegisterType': 'MODBUS_INPUT_REGISTER',
                    'variableType':  'integer',
                    'variableSize':  's16',
                    'minValue':      -400,
                    'maxValue':      400,
                    'divisor':       divisor,
                    'decimal':       1,
                    'unit':          '°C',
                    'shortUnit':     '°C',
                    'isWritable':    False,
                    'intDefaultValue': 0,
                    'stringDefaultValue': '',
                    'change':        1,
                    'modbusRegisterID': point_id,
                },
            }
        }

    def test_raw_integer_value_reaches_the_final_state_payload_divisor_scaled(self):
        point_id = 77001
        em = _make_em()
        em._api.fetch_bulk_points.return_value = self._raw_api_response(
            point_id, raw_int_value=225, divisor=10,
        )
        # An already-discovered, already-active entity, as it would be
        # after a real discover_points() call — update_all_states() only
        # updates entities already in active_entities_by_id, it does not
        # discover new ones.
        em.active_entities_by_id[point_id] = {
            'point_id':           point_id,
            'entity_type':        'sensor',
            'entity_category':    'diagnostic',
            'state_topic':        f'homeassistant/sensor/nibe_{point_id}/state',
            'availability_topic': f'homeassistant/sensor/nibe_{point_id}/available',
            'point_data':         {},
            'value_mapping':      None,
        }

        em.update_all_states()

        state_calls = [c for c in em.mqtt.publish.call_args_list
                       if c.args[0] == f'homeassistant/sensor/nibe_{point_id}/state']
        self.assertTrue(state_calls, "no state publish reached the real topic")
        # 225 / divisor 10 = 22.5, computed independently here as a literal
        # (not read back from apply_divisor's own return value) — the real
        # apply_divisor() runs inside the chain under test; its own
        # correctness is separately verified by its round-trip property tests.
        self.assertEqual(state_calls[-1].args[1], '22.5')

    def test_raw_isOk_false_publishes_offline_not_a_state_value(self):
        """isOk=False in the raw API value must reach the real offline
        availability publish, not a state value derived from garbage data."""
        point_id = 77002
        em = _make_em()
        response = self._raw_api_response(point_id, raw_int_value=0, divisor=10)
        response[str(point_id)]['value']['isOk'] = False
        em._api.fetch_bulk_points.return_value = response
        em.active_entities_by_id[point_id] = {
            'point_id':           point_id,
            'entity_type':        'sensor',
            'entity_category':    'diagnostic',
            'state_topic':        f'homeassistant/sensor/nibe_{point_id}/state',
            'availability_topic': f'homeassistant/sensor/nibe_{point_id}/available',
            'point_data':         {},
            'value_mapping':      None,
        }

        em.update_all_states()

        avail_calls = [c for c in em.mqtt.publish.call_args_list
                       if c.args[0] == f'homeassistant/sensor/nibe_{point_id}/available']
        self.assertTrue(avail_calls)
        self.assertEqual(avail_calls[-1].args[1], 'offline')
        state_calls = [c for c in em.mqtt.publish.call_args_list
                       if c.args[0] == f'homeassistant/sensor/nibe_{point_id}/state']
        self.assertEqual(state_calls, [])
