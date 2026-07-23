"""
test_api.py
===========
Nibe_api tests.
Part of the Nibe S-Series MQTT Bridge test suite.
Shared fixtures are in conftest.py.
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from hypothesis import example, given
from hypothesis import strategies as st

from conftest import (
    _make_em,
    _nibe_point_id,
)

class TestRetryDelay(unittest.TestCase):
    def setUp(self):
        from nibe_api import _retry_delay, _RETRY_BASE_S, _RETRY_MAX_S
        self.fn = _retry_delay
        self.cap = min(_RETRY_BASE_S, _RETRY_MAX_S)

    def test_non_negative(self):
        for _ in range(50): self.assertGreaterEqual(self.fn(), 0)  # noqa: E701

    def test_within_bounds(self):
        for _ in range(50): self.assertLessEqual(self.fn(), self.cap)  # noqa: E701

    def test_values_vary(self):
        d = [self.fn() for _ in range(20)]
        self.assertGreater(max(d) - min(d), 0)



class TestWritePointValidation(unittest.TestCase):
    def setUp(self):
        import ssl
        from nibe_api import NibeApiClient
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        self.c = NibeApiClient("https://192.0.2.1:8443/api/v1/devices/0",
                               "Basic dGVzdA==", ctx)

    def _ei(self, writable=True, lo=0, hi=100, degen=False):
        return {'is_writable': writable, 'is_degenerate_range': degen,
                'metadata': {'minValue': lo, 'maxValue': hi, 'isWritable': writable}}

    def _resp(self, body):
        r = MagicMock()
        r.read.return_value = json.dumps(body).encode()
        return r

    def test_non_writable_false(self):  self.assertFalse(self.c.write_point(1, 50,  self._ei(writable=False)))
    def test_below_min_false(self):     self.assertFalse(self.c.write_point(1, -10, self._ei()))
    def test_above_max_false(self):     self.assertFalse(self.c.write_point(1, 150, self._ei()))

    def test_at_min_boundary(self):
        with patch('urllib.request.urlopen', return_value=self._resp({"1": "modified"})):
            self.assertTrue(self.c.write_point(1, 0, self._ei()))

    def test_at_max_boundary(self):
        with patch('urllib.request.urlopen', return_value=self._resp({"1": "modified"})):
            self.assertTrue(self.c.write_point(1, 100, self._ei()))

    def test_degenerate_skips_range(self):
        with patch('urllib.request.urlopen', return_value=self._resp({"1": "modified"})):
            self.assertTrue(self.c.write_point(1, 999, self._ei(lo=0, hi=0, degen=True)))

    def test_modified_true(self):
        with patch('urllib.request.urlopen', return_value=self._resp({"5": "modified"})):
            self.assertTrue(self.c.write_point(5, 1, self._ei()))

    def test_isok_true(self):
        with patch('urllib.request.urlopen',
                   return_value=self._resp({"5": {"value": {"isOk": True}}})):
            self.assertTrue(self.c.write_point(5, 1, self._ei()))

    def test_isok_false(self):
        with patch('urllib.request.urlopen',
                   return_value=self._resp({"5": {"value": {"isOk": False}}})):
            self.assertFalse(self.c.write_point(5, 1, self._ei()))

    def test_read_only_response(self):
        with patch('urllib.request.urlopen',
                   return_value=self._resp({"5": "error: read only value"})):
            self.assertFalse(self.c.write_point(5, 1, self._ei()))

    def test_no_such_param(self):
        with patch('urllib.request.urlopen',
                   return_value=self._resp({"5": "error: no such param"})):
            self.assertFalse(self.c.write_point(5, 1, self._ei()))


if __name__ == '__main__':
    unittest.main(verbosity=2)



class TestWritePointValidationProperties(unittest.TestCase):
    """Hypothesis properties for write_point range validation."""

    def _client(self):
        from nibe_api import NibeApiClient
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return NibeApiClient('user:pass', 'https://host:8443', ctx)

    def _ei(self, min_val, max_val, degenerate=False):
        return {
            'is_writable': True,
            'metadata': {'minValue': min_val, 'maxValue': max_val,
                         'variableId': 100, 'isWritable': True,
                         'modbusRegisterType': 'MODBUS_HOLDING_REGISTER'},
            'is_degenerate_range': degenerate,
        }

    def _mock_resp(self, pid=100):
        import json as _json
        mock = MagicMock()
        mock.read.return_value = _json.dumps({str(pid): 'modified'}).encode()
        mock.__enter__ = lambda s: s
        mock.__exit__ = MagicMock(return_value=False)
        return mock

    @given(st.integers(min_value=-1000, max_value=1000),
           st.integers(min_value=0, max_value=100))
    @example(min_val=0,   width=1)    # binary register (0/1)
    @example(min_val=-300, width=600)  # temperature range (typical)
    @example(min_val=0,   width=100)  # percentage range
    def test_value_in_range_proceeds_to_network(self, min_val, width):
        """Any in-range value must always proceed to urlopen."""
        max_val = min_val + width
        value = min_val + (width // 2)
        client = self._client()
        with patch('urllib.request.urlopen', return_value=self._mock_resp()) as mock_open:
            client.write_point(100, value, self._ei(min_val, max_val))
        mock_open.assert_called_once()

    @given(st.integers(min_value=-1000, max_value=1000),
           st.integers(min_value=0, max_value=100))
    def test_value_at_min_proceeds(self, min_val, width):
        """Value exactly at min must always proceed (inclusive lower bound)."""
        client = self._client()
        with patch('urllib.request.urlopen', return_value=self._mock_resp()) as mock_open:
            client.write_point(100, min_val, self._ei(min_val, min_val + width))
        mock_open.assert_called_once()

    @given(st.integers(min_value=-1000, max_value=1000),
           st.integers(min_value=0, max_value=100))
    def test_value_at_max_proceeds(self, min_val, width):
        """Value exactly at max must always proceed (inclusive upper bound)."""
        client = self._client()
        max_val = min_val + width
        with patch('urllib.request.urlopen', return_value=self._mock_resp()) as mock_open:
            client.write_point(100, max_val, self._ei(min_val, max_val))
        mock_open.assert_called_once()

    @given(st.integers(min_value=-1000, max_value=1000),
           st.integers(min_value=1, max_value=100))
    def test_value_below_min_rejected_without_network(self, min_val, below):
        """Value below min must be rejected without calling urlopen."""
        client = self._client()
        with patch('urllib.request.urlopen') as mock_open:
            result = client.write_point(100, min_val - below,
                                        self._ei(min_val, min_val + 10))
        self.assertFalse(result)
        mock_open.assert_not_called()

    @given(st.integers(min_value=-1000, max_value=1000),
           st.integers(min_value=1, max_value=100))
    def test_value_above_max_rejected_without_network(self, max_val, above):
        """Value above max must be rejected without calling urlopen."""
        client = self._client()
        with patch('urllib.request.urlopen') as mock_open:
            result = client.write_point(100, max_val + above,
                                        self._ei(max_val - 10, max_val))
        self.assertFalse(result)
        mock_open.assert_not_called()

    @given(st.integers(min_value=-32768, max_value=32767))
    def test_degenerate_range_always_proceeds(self, value):
        """Degenerate range (min==max) always proceeds regardless of value."""
        client = self._client()
        with patch('urllib.request.urlopen', return_value=self._mock_resp()) as mock_open:
            client.write_point(100, value, self._ei(5, 5, degenerate=True))
        mock_open.assert_called_once()



class TestRequestRetry(unittest.TestCase):
    """Tests for NibeApiClient.request() — retry, backoff, error classification."""

    def setUp(self):
        import ssl
        from nibe_api import NibeApiClient
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        self.client = NibeApiClient(
            "https://192.0.2.1:8443/api/v1/devices/0",
            "Basic dGVzdA==", ctx,
        )

    def _ok_response(self, body: dict):
        r = MagicMock()
        r.read.return_value = json.dumps(body).encode()
        return r

    def _http_error(self, code: int):
        import urllib.error
        e = urllib.error.HTTPError(url='', code=code, msg='err',
                                   hdrs=None, fp=None)
        return e

    # ── successful request ────────────────────────────────────────────────────

    def test_successful_request_returns_json(self):
        with patch('urllib.request.urlopen',
                   return_value=self._ok_response({'ok': True})):
            result = self.client.request('https://192.0.2.1:8443/test')
        self.assertEqual(result, {'ok': True})

    # ── HTTP 401/403 — auth errors raise immediately, no retry ───────────────

    def test_http_401_raises_no_retry(self):
        import urllib.error
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(401)):
            with self.assertRaises(urllib.error.HTTPError):
                self.client.request('https://192.0.2.1:8443/test')

    def test_http_403_raises_no_retry(self):
        import urllib.error
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(403)):
            with self.assertRaises(urllib.error.HTTPError):
                self.client.request('https://192.0.2.1:8443/test')

    def test_auth_error_does_not_sleep(self):
        """Auth errors must not sleep — they are permanent failures."""
        import urllib.error
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(401)), \
             patch('nibe_api.time.sleep') as mock_sleep:
            try:
                self.client.request('https://192.0.2.1:8443/test')
            except urllib.error.HTTPError:
                pass
        mock_sleep.assert_not_called()

    # ── HTTP 404 — raises immediately, no retry ───────────────────────────────

    def test_http_404_raises_no_retry(self):
        import urllib.error
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(404)):
            with self.assertRaises(urllib.error.HTTPError):
                self.client.request('https://192.0.2.1:8443/test')

    # ── HTTP 500 — retries once with jitter sleep, then returns None ──────────

    def test_http_500_retries_once(self):
        import urllib.error
        call_count = []
        def side_effect(*a, **kw):
            call_count.append(1)
            raise urllib.error.HTTPError('', 500, 'err', None, None)

        with patch('urllib.request.urlopen', side_effect=side_effect), \
             patch('nibe_api.time.sleep'):
            result = self.client.request('https://192.0.2.1:8443/test')

        self.assertIsNone(result)
        self.assertEqual(len(call_count), 2,
                         "Should have been called twice (initial + one retry)")

    def test_http_500_sleeps_before_retry(self):
        import urllib.error
        with patch('urllib.request.urlopen',
                   side_effect=urllib.error.HTTPError('', 500, 'err', None, None)), \
             patch('nibe_api.time.sleep') as mock_sleep:
            self.client.request('https://192.0.2.1:8443/test')
        mock_sleep.assert_called_once()
        delay = mock_sleep.call_args[0][0]
        self.assertGreaterEqual(delay, 0)
        self.assertLessEqual(delay, 2.0)   # within _RETRY_BASE_S cap

    def test_http_500_no_second_retry(self):
        """Only one retry total — the loop never attempts more than two calls."""
        import urllib.error
        call_count = []
        def side_effect(*a, **kw):
            call_count.append(1)
            raise urllib.error.HTTPError('', 500, 'err', None, None)

        with patch('urllib.request.urlopen', side_effect=side_effect), \
             patch('nibe_api.time.sleep'):
            self.client.request('https://192.0.2.1:8443/test')

        self.assertEqual(len(call_count), 2)  # never more than 2

    # ── Network exception — retries once, then returns None ──────────────────

    def test_network_exception_retries_once(self):
        call_count = []
        def side_effect(*a, **kw):
            call_count.append(1)
            raise ConnectionError("network down")

        with patch('urllib.request.urlopen', side_effect=side_effect), \
             patch('nibe_api.time.sleep'):
            result = self.client.request('https://192.0.2.1:8443/test')

        self.assertIsNone(result)
        self.assertEqual(len(call_count), 2)

    def test_network_exception_sleeps_before_retry(self):
        with patch('urllib.request.urlopen',
                   side_effect=ConnectionError("down")), \
             patch('nibe_api.time.sleep') as mock_sleep:
            self.client.request('https://192.0.2.1:8443/test')
        mock_sleep.assert_called_once()

    def test_retry_succeeds_on_second_attempt(self):
        """If the first call fails but the retry succeeds, return the value."""
        import urllib.error
        attempts = []
        def side_effect(*a, **kw):
            attempts.append(1)
            if len(attempts) == 1:
                raise urllib.error.HTTPError('', 503, 'unavailable', None, None)
            return self._ok_response({'recovered': True})

        with patch('urllib.request.urlopen', side_effect=side_effect), \
             patch('nibe_api.time.sleep'):
            result = self.client.request('https://192.0.2.1:8443/test')

        self.assertEqual(result, {'recovered': True})

    def test_no_private_retry_parameter(self):
        """The _retry flag was removed when the implementation switched from
        recursion to a loop.  Passing it must now raise TypeError so callers
        don't silently rely on a removed implementation detail."""
        with self.assertRaises(TypeError):
            self.client.request('https://192.0.2.1:8443/test', _retry=False)



class TestRequestRetryProperties(unittest.TestCase):
    """Hypothesis properties for NibeApiClient.request() retry logic."""

    def _client(self):
        from nibe_api import NibeApiClient
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return NibeApiClient('user:pass', 'https://host:8443', ctx)

    def _mock_resp(self, body=None):
        import json as _json
        mock = MagicMock()
        mock.read.return_value = _json.dumps(body or {}).encode()
        mock.__enter__ = lambda s: s
        mock.__exit__ = MagicMock(return_value=False)
        return mock

    @given(st.dictionaries(
        st.text(min_size=1, max_size=5, alphabet=st.characters(
            categories=['L',])),
        st.integers(), max_size=3))
    def test_success_no_retry(self, body):
        """Successful first call: urlopen called exactly once, no sleep."""
        client = self._client()
        with patch('urllib.request.urlopen', return_value=self._mock_resp(body)),              patch('time.sleep') as mock_sleep:
            result = client.request('https://host/api/v1/devices/test/points')
        mock_sleep.assert_not_called()
        self.assertEqual(result, body)

    def test_first_failure_triggers_one_retry(self):
        """Transient URLError: exactly 2 urlopen calls, 1 sleep."""
        import urllib.error
        client = self._client()
        err = urllib.error.URLError('timeout')
        calls = [err, self._mock_resp({'ok': 1})]
        with patch('urllib.request.urlopen', side_effect=calls),              patch('time.sleep') as mock_sleep:
            client.request('https://host/api/v1/devices/test/points')
        self.assertEqual(mock_sleep.call_count, 1)

    def test_both_failures_returns_none(self):
        """Two URLErrors: returns None after retry."""
        import urllib.error
        client = self._client()
        err = urllib.error.URLError('timeout')
        with patch('urllib.request.urlopen', side_effect=[err, err]),              patch('time.sleep'):
            result = client.request('https://host/api/v1/devices/test/points')
        self.assertIsNone(result)

    @given(st.integers(min_value=401, max_value=403).filter(
        lambda c: c in (401, 403)))
    def test_auth_error_never_retried(self, code):
        """Auth errors must never trigger a retry."""
        import urllib.error
        client = self._client()
        err = urllib.error.HTTPError('url', code, 'Auth', {}, None)
        with patch('urllib.request.urlopen', side_effect=err),              patch('time.sleep') as mock_sleep:
            try:
                client.request('https://host/api/v1/devices/test/points')
            except urllib.error.HTTPError:
                pass
        mock_sleep.assert_not_called()

    def test_404_never_retried(self):
        """HTTP 404 must never trigger a retry."""
        import urllib.error
        client = self._client()
        err = urllib.error.HTTPError('url', 404, 'Not Found', {}, None)
        with patch('urllib.request.urlopen', side_effect=err),              patch('time.sleep') as mock_sleep:
            try:
                client.request('https://host/api/v1/devices/test/points')
            except urllib.error.HTTPError:
                pass
        mock_sleep.assert_not_called()


# ===========================================================================
# 15. Pending write guard (_update_entity_state)
# ===========================================================================


class TestWriteValidationBoundaries(unittest.TestCase):
    """Tests for the double-layer range validation:
    _parse_command_payload (client-side) and write_point (API-side).
    Both layers use the same entity_info, so they fail together if
    entity_info is wrong — these tests verify the logic is consistent."""

    def setUp(self):
        self.em = _make_em()

    def _ei(self, lo, hi, divisor=1, degen=False):
        return {
            'point_id': 1000, 'entity_type': 'number',
            'metadata': {'divisor': divisor, 'minValue': lo, 'maxValue': hi,
                         'isWritable': True},
            'state_topic': 'nibe/s/1000',
            'is_degenerate_range': degen,
        }

    def test_boundary_values_accepted(self):
        """min and max boundary values must pass both validation layers."""
        import ssl
        from nibe_api import NibeApiClient
        from unittest.mock import patch, MagicMock
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        client = NibeApiClient("https://192.0.2.1:8443/api/v1/devices/0",
                               "Basic dGVzdA==", ctx)
        ei_api = {'is_writable': True, 'is_degenerate_range': False,
                  'metadata': {'minValue': 150, 'maxValue': 300, 'isWritable': True}}
        r = MagicMock()
        r.read.return_value = json.dumps({"1000": "modified"}).encode()
        with patch('urllib.request.urlopen', return_value=r):
            self.assertTrue(client.write_point(1000, 150, ei_api))
            self.assertTrue(client.write_point(1000, 300, ei_api))

    def test_client_and_api_reject_same_out_of_range_value(self):
        """A value rejected by _parse_command_payload must also be rejected
        by write_point — both use the same minValue/maxValue from entity_info."""
        import ssl
        from nibe_api import NibeApiClient
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        client = NibeApiClient("https://192.0.2.1:8443/api/v1/devices/0",
                               "Basic dGVzdA==", ctx)
        ei_parse = self._ei(lo=150, hi=300, divisor=10)
        # Client-side: 1000.0 / 10 = 100 which is below minValue=150
        parse_result = self.em._parse_command_payload("100.0", ei_parse, "t")
        self.assertIsNone(parse_result, "Client-side should reject 100.0 (below min)")

        # API-side: raw value 100 is below minValue=150
        ei_api = {'is_writable': True, 'is_degenerate_range': False,
                  'metadata': {'minValue': 150, 'maxValue': 300, 'isWritable': True}}
        api_result = client.write_point(1000, 100, ei_api)
        self.assertFalse(api_result, "API-side should reject raw 100 (below min)")

    def test_degenerate_range_bypasses_both_layers(self):
        """is_degenerate_range=True must bypass range checks in both
        _parse_command_payload and write_point."""
        import ssl
        from nibe_api import NibeApiClient
        from unittest.mock import patch, MagicMock
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        client = NibeApiClient("https://192.0.2.1:8443/api/v1/devices/0",
                               "Basic dGVzdA==", ctx)
        # Client side: value 999 would normally exceed maxValue=0
        ei_parse = self._ei(lo=0, hi=0, degen=True)
        parse_result = self.em._parse_command_payload("999", ei_parse, "t")
        self.assertEqual(parse_result, 999)

        # API side
        r = MagicMock()
        r.read.return_value = json.dumps({"1000": "modified"}).encode()
        ei_api = {'is_writable': True, 'is_degenerate_range': True,
                  'metadata': {'minValue': 0, 'maxValue': 0, 'isWritable': True}}
        with patch('urllib.request.urlopen', return_value=r):
            self.assertTrue(client.write_point(1000, 999, ei_api))

    def test_divisor_scaling_does_not_cause_range_false_negative(self):
        """After divisor scaling, the raw value must be range-checked against
        the raw minValue/maxValue (not the display value).

        Example: display=22.5, divisor=10 → raw=225.
        minValue=150, maxValue=300 in raw units → 225 is valid.
        The client must NOT compare 22.5 against 150–300 (that would reject it).
        """
        ei = self._ei(lo=150, hi=300, divisor=10)
        result = self.em._parse_command_payload("22.5", ei, "t")
        self.assertEqual(result, 225,
                         "22.5 * 10 = 225 which is within [150,300] — must be accepted")

    def test_divisor_scaling_does_not_cause_range_false_positive(self):
        """A display value that looks in-range but converts to an out-of-range
        raw value must be rejected.

        Example: display=10.0, divisor=10 → raw=100 < minValue=150.
        """
        ei = self._ei(lo=150, hi=300, divisor=10)
        result = self.em._parse_command_payload("10.0", ei, "t")
        self.assertIsNone(result,
                          "10.0 * 10 = 100 which is below minValue=150 — must be rejected")

# ===========================================================================
# 21. MqttDiscoveryPublisher — config builders
# ===========================================================================


class TestNibeApiClientMethods(unittest.TestCase):
    """Tests for NibeApiClient high-level methods using a mocked urlopen."""

    def setUp(self):
        import ssl
        from nibe_api import NibeApiClient
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        self.client = NibeApiClient(
            "https://192.0.2.1:8443/api/v1/devices/0",
            "Basic dGVzdA==", ctx,
        )

    def _ok(self, body: dict):
        r = MagicMock()
        r.read.return_value = json.dumps(body).encode()
        return r

    def _http_error(self, code: int, body: str = ""):
        import urllib.error
        import io
        fp = io.BytesIO(body.encode()) if body else None
        e  = urllib.error.HTTPError(url='', code=code, msg='err', hdrs=None, fp=fp)
        if body:
            e.read = lambda: body.encode()  # type: ignore[method-assign, misc, assignment]
        return e

    def _entity_info(self, writable=True, min_val=0, max_val=100, degenerate=False):
        return {
            'is_writable':       writable,
            'is_degenerate_range': degenerate,
            'metadata': {
                'minValue': min_val,
                'maxValue': max_val,
            },
        }

    # ── fetch_point ──────────────────────────────────────────────────────────

    def test_fetch_point_returns_dict_on_success(self):
        with patch('urllib.request.urlopen',
                   return_value=self._ok({'title': 'Temp'})):
            result = self.client.fetch_point(1000)
        self.assertEqual(result, {'title': 'Temp'})

    def test_fetch_point_404_returns_none(self):
        """HTTP 404 returns None — dynamic point inactive."""
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(404)):
            result = self.client.fetch_point(1000)
        self.assertIsNone(result)

    def test_fetch_point_non_404_http_error_reraises(self):
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(500)), \
             patch('nibe_api.time.sleep'):
            result = self.client.fetch_point(1000)
        self.assertIsNone(result)

    def test_fetch_point_401_reraises_to_caller(self):
        """A 401 on a single-point fetch must propagate — request() re-raises
        auth errors and fetch_point's except block re-raises anything not 404
        (line 176). Callers like post-write failure revert need to see it."""
        import urllib.error
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(401)), \
             patch('nibe_api.time.sleep'):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.client.fetch_point(1000)
        self.assertEqual(ctx.exception.code, 401)

    # ── fetch_notifications ──────────────────────────────────────────────────

    def test_fetch_notifications_returns_alarm_list(self):
        alarms = [{'alarmId': 1, 'description': 'test'}]
        with patch('urllib.request.urlopen',
                   return_value=self._ok({'alarms': alarms})):
            result = self.client.fetch_notifications()
        self.assertEqual(result, alarms)

    def test_fetch_notifications_returns_empty_list_when_no_alarms(self):
        with patch('urllib.request.urlopen',
                   return_value=self._ok({'alarms': []})):
            result = self.client.fetch_notifications()
        self.assertEqual(result, [])

    def test_fetch_notifications_returns_none_on_network_error(self):
        with patch('urllib.request.urlopen',
                   side_effect=ConnectionError('down')), \
             patch('nibe_api.time.sleep'):
            result = self.client.fetch_notifications()
        self.assertIsNone(result)

    def test_fetch_notifications_url(self):
        """fetch_notifications must hit <base_url>/notifications, not a
        reconstructed path via the old _device_root split."""
        captured = {}
        def fake_urlopen(req, _context=None, _timeout=None, **_kw):
            captured['url'] = req.full_url
            r = MagicMock()
            r.read.return_value = json.dumps({'alarms': []}).encode()
            return r
        with patch('urllib.request.urlopen', side_effect=fake_urlopen):
            self.client.fetch_notifications()
        self.assertEqual(
            captured['url'],
            'https://192.0.2.1:8443/api/v1/devices/0/notifications',
        )

    def test_reset_notifications_url(self):
        """reset_notifications must DELETE <base_url>/notifications."""
        captured = {}
        def fake_urlopen(req, _context=None, _timeout=None, **_kw):
            captured['url']    = req.full_url
            captured['method'] = req.get_method()
            return MagicMock()
        with patch('urllib.request.urlopen', side_effect=fake_urlopen):
            self.client.reset_notifications()
        self.assertEqual(
            captured['url'],
            'https://192.0.2.1:8443/api/v1/devices/0/notifications',
        )
        self.assertEqual(captured['method'], 'DELETE')

    def test_write_device_mode_url(self):
        """write_device_mode must POST to <base_url>/{mode_type}."""
        captured = {}
        def fake_urlopen(req, _context=None, _timeout=None, **_kw):
            captured['url']    = req.full_url
            captured['method'] = req.get_method()
            return MagicMock()
        with patch('urllib.request.urlopen', side_effect=fake_urlopen):
            self.client.write_device_mode('aidmode', 'on')
        self.assertEqual(
            captured['url'],
            'https://192.0.2.1:8443/api/v1/devices/0/aidmode',
        )
        self.assertEqual(captured['method'], 'POST')

    # ── patch_points — guard rails ───────────────────────────────────────────

    def test_write_non_writable_returns_false(self):
        result = self.client.write_point(1000, 50, self._entity_info(writable=False))
        self.assertFalse(result)

    def test_write_below_min_returns_false(self):
        result = self.client.write_point(1000, -1, self._entity_info(min_val=0, max_val=100))
        self.assertFalse(result)

    def test_write_above_max_returns_false(self):
        result = self.client.write_point(1000, 101, self._entity_info(min_val=0, max_val=100))
        self.assertFalse(result)

    def test_write_degenerate_range_bypasses_range_check(self):
        with patch('urllib.request.urlopen',
                   return_value=self._ok({'1000': 'modified'})):
            result = self.client.write_point(
                1000, 99999, self._entity_info(min_val=0, max_val=0, degenerate=True)
            )
        self.assertTrue(result)

    # ── patch_points — success responses ────────────────────────────────────

    def test_write_modified_string_returns_true(self):
        with patch('urllib.request.urlopen',
                   return_value=self._ok({'1000': 'modified'})):
            result = self.client.write_point(1000, 50, self._entity_info())
        self.assertTrue(result)

    def test_write_full_object_response_isok_true_returns_true(self):
        body = {'1000': {'value': {'isOk': True, 'variableId': 1000, 'integerValue': 50}}}
        with patch('urllib.request.urlopen', return_value=self._ok(body)):
            result = self.client.write_point(1000, 50, self._entity_info())
        self.assertTrue(result)

    def test_write_full_object_response_isok_false_returns_false(self):
        body = {'1000': {'value': {'isOk': False}}}
        with patch('urllib.request.urlopen', return_value=self._ok(body)):
            result = self.client.write_point(1000, 50, self._entity_info())
        self.assertFalse(result)

    def test_write_no_such_param_returns_false(self):
        with patch('urllib.request.urlopen',
                   return_value=self._ok({'1000': 'error: no such param'})):
            result = self.client.write_point(1000, 50, self._entity_info())
        self.assertFalse(result)

    def test_write_read_only_value_returns_false(self):
        with patch('urllib.request.urlopen',
                   return_value=self._ok({'1000': 'error: read only value'})):
            result = self.client.write_point(1000, 50, self._entity_info())
        self.assertFalse(result)

    # ── patch_points — HTTP error responses ─────────────────────────────────

    def test_write_http_400_returns_false(self):
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(400, 'bad request')):
            result = self.client.write_point(1000, 50, self._entity_info())
        self.assertFalse(result)

    def test_write_http_401_returns_false(self):
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(401)):
            result = self.client.write_point(1000, 50, self._entity_info())
        self.assertFalse(result)

    def test_write_http_403_returns_false(self):
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(403)):
            result = self.client.write_point(1000, 50, self._entity_info())
        self.assertFalse(result)

    def test_write_network_exception_returns_false(self):
        with patch('urllib.request.urlopen',
                   side_effect=ConnectionError('down')):
            result = self.client.write_point(1000, 50, self._entity_info())
        self.assertFalse(result)

    # ── reset_notifications ──────────────────────────────────────────────────

    def test_reset_notifications_success_returns_true(self):
        with patch('urllib.request.urlopen', return_value=MagicMock()):
            result = self.client.reset_notifications()
        self.assertTrue(result)

    def test_reset_notifications_405_returns_false(self):
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(405)):
            result = self.client.reset_notifications()
        self.assertFalse(result)

    def test_reset_notifications_401_returns_false(self):
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(401)):
            result = self.client.reset_notifications()
        self.assertFalse(result)

    def test_reset_notifications_network_exception_returns_false(self):
        with patch('urllib.request.urlopen',
                   side_effect=ConnectionError('down')):
            result = self.client.reset_notifications()
        self.assertFalse(result)

    # ── write_device_mode ────────────────────────────────────────────────────

    def test_write_device_mode_success_returns_true(self):
        with patch('urllib.request.urlopen', return_value=MagicMock()):
            result = self.client.write_device_mode('aidmode', 'on')
        self.assertTrue(result)

    def test_write_device_mode_400_returns_false(self):
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(400, 'bad value')):
            result = self.client.write_device_mode('aidmode', 'invalid')
        self.assertFalse(result)

    def test_write_device_mode_401_returns_false(self):
        with patch('urllib.request.urlopen',
                   side_effect=self._http_error(401)):
            result = self.client.write_device_mode('smartmode', 'away')
        self.assertFalse(result)

    def test_write_device_mode_network_exception_returns_false(self):
        with patch('urllib.request.urlopen',
                   side_effect=ConnectionError('down')):
            result = self.client.write_device_mode('aidmode', 'on')
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# NibeApiClient.request Hypothesis properties (nibe_api.py)
# ---------------------------------------------------------------------------


class TestNibeApiRequestProperties(unittest.TestCase):
    """Hypothesis properties for NibeApiClient.request."""

    def _client(self):
        from nibe_api import NibeApiClient
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return NibeApiClient('user:pass', 'https://host:8443', ctx)

    def _mock_response(self, body_dict):
        import json as _json
        mock_resp = MagicMock()
        mock_resp.read.return_value = _json.dumps(body_dict).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    @given(st.integers(min_value=401, max_value=403).filter(lambda c: c in (401, 403)))
    def test_auth_error_always_raises(self, code):
        """HTTP 401/403 must always raise HTTPError — never return None."""
        import urllib.error
        client = self._client()
        err = urllib.error.HTTPError('url', code, 'Auth', {}, None)
        with patch('urllib.request.urlopen', side_effect=err):
            with self.assertRaises(urllib.error.HTTPError):
                client.request('https://host/api/v1/devices/test/points')

    def test_404_always_raises(self):
        """HTTP 404 must always raise HTTPError (dynamic point inactive)."""
        import urllib.error
        client = self._client()
        err = urllib.error.HTTPError('url', 404, 'Not Found', {}, None)
        with patch('urllib.request.urlopen', side_effect=err):
            with self.assertRaises(urllib.error.HTTPError):
                client.request('https://host/api/v1/devices/test/points/99999')

    @given(st.integers(min_value=500, max_value=599))
    def test_server_error_returns_none(self, code):
        """Non-auth HTTP errors (5xx) return None after retry."""
        import urllib.error
        client = self._client()
        err = urllib.error.HTTPError('url', code, 'Server Error', {}, None)
        with patch('urllib.request.urlopen', side_effect=err), \
             patch('time.sleep'):
            result = client.request('https://host/api/v1/devices/test/points')
        self.assertIsNone(result)

    def test_url_error_returns_none(self):
        """Network errors return None after retry."""
        import urllib.error
        client = self._client()
        with patch('urllib.request.urlopen',
                   side_effect=urllib.error.URLError('Connection refused')), \
             patch('time.sleep'):
            result = client.request('https://host/api/v1/devices/test/points')
        self.assertIsNone(result)

    @given(st.dictionaries(
        st.text(min_size=1, max_size=10, alphabet=st.characters(
            categories=['L',])),
        st.integers(),
        max_size=5,
    ))
    def test_success_returns_parsed_json(self, body):
        """Successful response returns the parsed JSON dict."""
        client = self._client()
        with patch('urllib.request.urlopen', return_value=self._mock_response(body)):
            result = client.request('https://host/api/v1/devices/test/points')
        self.assertEqual(result, body)

    def test_json_error_returns_none(self):
        """Malformed JSON response returns None gracefully."""
        client = self._client()
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'not json {'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch('urllib.request.urlopen', return_value=mock_resp), \
             patch('time.sleep'):
            result = client.request('https://host/api/v1/devices/test/points')
        self.assertIsNone(result)

    @given(st.dictionaries(
        st.text(min_size=1, max_size=10, alphabet=st.characters(
            categories=['L',])),
        st.integers(),
        max_size=5,
    ))
    def test_result_always_dict_or_none(self, body):
        """request() always returns dict or None, never raises for valid responses."""
        client = self._client()
        with patch('urllib.request.urlopen', return_value=self._mock_response(body)):
            result = client.request('https://host/api/v1/devices/test/points')
        self.assertIn(type(result), (dict, type(None)))


# ---------------------------------------------------------------------------
# NibeApiClient fetch/write method properties (nibe_api.py)
# ---------------------------------------------------------------------------


class TestNibeApiFetchPointProperties(unittest.TestCase):
    """Hypothesis properties for NibeApiClient.fetch_point.

    fetch_point returns None for both network errors and HTTP 404.
    HTTP 404 = dynamic point inactive (firmware deviation #3).
    Callers treat None uniformly as "point unavailable".
    """

    def _client(self):
        from nibe_api import NibeApiClient
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return NibeApiClient('user:pass', 'https://host:8443', ctx)

    def test_404_returns_none(self):
        """HTTP 404 must return None — dynamic point inactive."""
        import urllib.error
        client = self._client()
        err = urllib.error.HTTPError('url', 404, 'Not Found', {}, None)
        with patch('urllib.request.urlopen', side_effect=err):
            result = client.fetch_point(99999)
        self.assertIsNone(result)

    def test_404_never_raises(self):
        """HTTP 404 must never propagate — always caught."""
        import urllib.error
        client = self._client()
        err = urllib.error.HTTPError('url', 404, 'Not Found', {}, None)
        with patch('urllib.request.urlopen', side_effect=err):
            client.fetch_point(99999)  # must not raise

    @given(_nibe_point_id)
    @example(pid=10001)   # typical dynamic point range
    @example(pid=99999)   # large pid
    @example(pid=1)       # small pid
    def test_404_result_is_always_none(self, pid):
        """HTTP 404 for any point_id always returns None."""
        import urllib.error
        from nibe_api import NibeApiClient
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        client = NibeApiClient('user:pass', 'https://host:8443', ctx)
        err = urllib.error.HTTPError('url', 404, 'Not Found', {}, None)
        with patch('urllib.request.urlopen', side_effect=err):
            result = client.fetch_point(pid)
        self.assertIsNone(result)

    @given(st.integers(min_value=401, max_value=403).filter(lambda c: c in (401, 403)))
    def test_auth_error_propagates(self, code):
        """401/403 must always propagate through fetch_point."""
        import urllib.error
        client = self._client()
        err = urllib.error.HTTPError('url', code, 'Auth', {}, None)
        with patch('urllib.request.urlopen', side_effect=err):
            with self.assertRaises(urllib.error.HTTPError):
                client.fetch_point(100)

    def test_success_returns_dict(self):
        """Successful response returns the parsed JSON dict."""
        import json as _json
        client = self._client()
        body = {'variableId': 100, 'value': {'integerValue': 42}}
        mock_resp = MagicMock()
        mock_resp.read.return_value = _json.dumps(body).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch('urllib.request.urlopen', return_value=mock_resp):
            result = client.fetch_point(100)
        self.assertEqual(result, body)



class TestNibeApiFetchNotificationsProperties(unittest.TestCase):
    """Hypothesis properties for NibeApiClient.fetch_notifications."""

    def _client(self):
        from nibe_api import NibeApiClient
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return NibeApiClient('user:pass', 'https://host:8443', ctx)

    def _mock_resp(self, body):
        import json as _json
        mock = MagicMock()
        mock.read.return_value = _json.dumps(body).encode()
        mock.__enter__ = lambda s: s
        mock.__exit__ = MagicMock(return_value=False)
        return mock

    def test_none_from_request_returns_none(self):
        """When request() returns None, fetch_notifications must return None."""
        client = self._client()
        with patch.object(client, 'request', return_value=None):
            result = client.fetch_notifications()
        self.assertIsNone(result)

    @given(st.lists(st.dictionaries(
        st.text(max_size=10), st.text(max_size=10), max_size=3), max_size=5))
    def test_alarms_key_returned(self, alarms):
        """Response with 'alarms' key → returns the alarms list."""
        client = self._client()
        with patch.object(client, 'request', return_value={'alarms': alarms}):
            result = client.fetch_notifications()
        self.assertEqual(result, alarms)

    def test_missing_alarms_key_returns_empty_list(self):
        """Response without 'alarms' key → returns []."""
        client = self._client()
        with patch.object(client, 'request', return_value={'other': 'data'}):
            result = client.fetch_notifications()
        self.assertEqual(result, [])

    def test_result_always_list_or_none(self):
        """fetch_notifications always returns list or None."""
        client = self._client()
        for response in [None, {'alarms': []}, {'alarms': [{'id': 1}]}, {}]:
            with patch.object(client, 'request', return_value=response):
                result = client.fetch_notifications()
            self.assertIn(type(result), (list, type(None)))



class TestNibeApiWritePointProperties(unittest.TestCase):
    """Hypothesis properties for NibeApiClient.write_point."""

    def _client(self):
        from nibe_api import NibeApiClient
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return NibeApiClient('user:pass', 'https://host:8443', ctx)

    def _entity_info(self, min_val=0, max_val=100):
        return {
            'is_writable': True,
            'metadata': {
                'minValue': min_val, 'maxValue': max_val,
                'variableId': 100, 'isWritable': True,
                'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
            },
            'is_degenerate_range': False,
        }

    def test_always_returns_bool(self):
        """write_point must always return a bool."""
        client = self._client()
        import json as _json
        resp = MagicMock()
        resp.read.return_value = _json.dumps({'100': 'modified'}).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        with patch('urllib.request.urlopen', return_value=resp):
            result = client.write_point(100, 50, self._entity_info())
        self.assertIsInstance(result, bool)

    def test_modified_response_returns_true(self):
        """Firmware 'modified' response must return True."""
        client = self._client()
        import json as _json
        resp = MagicMock()
        resp.read.return_value = _json.dumps({'100': 'modified'}).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        with patch('urllib.request.urlopen', return_value=resp):
            result = client.write_point(100, 50, self._entity_info())
        self.assertTrue(result)

    def test_full_object_isok_response_returns_true(self):
        """Firmware full-object response with isOk=True must return True."""
        client = self._client()
        import json as _json
        resp = MagicMock()
        resp.read.return_value = _json.dumps(
            {'100': {'value': {'isOk': True}}}).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        with patch('urllib.request.urlopen', return_value=resp):
            result = client.write_point(100, 50, self._entity_info())
        self.assertTrue(result)

    @given(st.integers(min_value=0, max_value=100))
    def test_value_below_min_returns_false(self, excess):
        """Value below minimum must always return False without network call."""
        client = self._client()
        with patch('urllib.request.urlopen') as mock_open:
            result = client.write_point(100, -(excess + 1), self._entity_info(0, 100))
        self.assertFalse(result)
        mock_open.assert_not_called()

    @given(st.integers(min_value=1, max_value=100))
    def test_value_above_max_returns_false(self, excess):
        """Value above maximum must always return False without network call."""
        client = self._client()
        with patch('urllib.request.urlopen') as mock_open:
            result = client.write_point(100, 100 + excess, self._entity_info(0, 100))
        self.assertFalse(result)
        mock_open.assert_not_called()


# ---------------------------------------------------------------------------
# NibeApiClient.write_device_mode properties (nibe_api.py)
# ---------------------------------------------------------------------------


class TestNibeApiWriteDeviceModeProperties(unittest.TestCase):
    """Hypothesis properties for NibeApiClient.write_device_mode.

    The critical hardware-confirmed property: JSON body uses mode_type as key.
    e.g. {"aidmode": "on"} not {"value": "on"} — firmware-specific format.
    """

    def _client(self):
        from nibe_api import NibeApiClient
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return NibeApiClient('user:pass', 'https://host:8443', ctx)

    @given(st.sampled_from(['aidmode', 'smartmode']),
           st.text(max_size=20))
    @example(mode_type='aidmode',   value='on')   # confirmed working on hardware
    @example(mode_type='aidmode',   value='off')
    @example(mode_type='smartmode', value='normal')  # confirmed working on hardware
    @example(mode_type='smartmode', value='away')    # confirmed working on hardware
    def test_json_body_key_equals_mode_type(self, mode_type, value):
        """JSON body must always use mode_type as the key — not 'value' or 'mode'."""
        import json as _json
        client = self._client()
        captured = []

        def fake_urlopen(req, **kw):
            captured.append(_json.loads(req.data.decode()))
            mock = MagicMock()
            mock.__enter__ = lambda s: s
            mock.__exit__ = MagicMock(return_value=False)
            return mock

        with patch('urllib.request.urlopen', side_effect=fake_urlopen):
            client.write_device_mode(mode_type, value)

        self.assertTrue(captured, "urlopen was never called")
        self.assertIn(mode_type, captured[0],
            f"JSON body {captured[0]} does not contain key '{mode_type}'")

    @given(st.sampled_from(['aidmode', 'smartmode']),
           st.text(max_size=20))
    def test_json_body_has_exactly_one_key(self, mode_type, value):
        """JSON body must have exactly one key — no extra fields."""
        import json as _json
        client = self._client()
        captured = []

        def fake_urlopen(req, **kw):
            captured.append(_json.loads(req.data.decode()))
            mock = MagicMock()
            mock.__enter__ = lambda s: s
            mock.__exit__ = MagicMock(return_value=False)
            return mock

        with patch('urllib.request.urlopen', side_effect=fake_urlopen):
            client.write_device_mode(mode_type, value)

        if captured:
            self.assertEqual(len(captured[0]), 1)

    @given(st.sampled_from(['aidmode', 'smartmode']),
           st.text(max_size=20))
    def test_json_body_value_matches_input(self, mode_type, value):
        """The value in the JSON body must exactly match the input value."""
        import json as _json
        client = self._client()
        captured = []

        def fake_urlopen(req, **kw):
            captured.append(_json.loads(req.data.decode()))
            mock = MagicMock()
            mock.__enter__ = lambda s: s
            mock.__exit__ = MagicMock(return_value=False)
            return mock

        with patch('urllib.request.urlopen', side_effect=fake_urlopen):
            client.write_device_mode(mode_type, value)

        if captured:
            self.assertEqual(captured[0][mode_type], value)

    @given(st.sampled_from(['aidmode', 'smartmode']),
           st.text(max_size=20))
    def test_success_returns_true(self, mode_type, value):
        """Successful POST returns True."""
        client = self._client()
        mock = MagicMock()
        mock.__enter__ = lambda s: s
        mock.__exit__ = MagicMock(return_value=False)
        with patch('urllib.request.urlopen', return_value=mock):
            result = client.write_device_mode(mode_type, value)
        self.assertTrue(result)

    @given(st.sampled_from(['aidmode', 'smartmode']),
           st.text(max_size=20),
           st.integers(min_value=400, max_value=599))
    def test_http_error_returns_false(self, mode_type, value, code):
        """Any HTTP error returns False."""
        import urllib.error
        client = self._client()
        err = urllib.error.HTTPError('url', code, 'Error', {}, None)
        with patch('urllib.request.urlopen', side_effect=err):
            result = client.write_device_mode(mode_type, value)
        self.assertFalse(result)

    @given(st.sampled_from(['aidmode', 'smartmode']),
           st.text(max_size=20))
    def test_always_returns_bool(self, mode_type, value):
        """write_device_mode always returns a bool."""
        client = self._client()
        mock = MagicMock()
        mock.__enter__ = lambda s: s
        mock.__exit__ = MagicMock(return_value=False)
        with patch('urllib.request.urlopen', return_value=mock):
            result = client.write_device_mode(mode_type, value)
        self.assertIsInstance(result, bool)

    @given(st.sampled_from(['aidmode', 'smartmode']),
           st.text(max_size=20))
    def test_url_contains_mode_type(self, mode_type, value):
        """The URL must always contain mode_type as the path segment."""
        client = self._client()
        captured_urls = []


        def fake_urlopen(req, **kw):
            captured_urls.append(req.full_url)
            mock = MagicMock()
            mock.__enter__ = lambda s: s
            mock.__exit__ = MagicMock(return_value=False)
            return mock

        with patch('urllib.request.urlopen', side_effect=fake_urlopen):
            client.write_device_mode(mode_type, value)

        if captured_urls:
            self.assertIn(mode_type, captured_urls[0])


# ---------------------------------------------------------------------------
# NibeApiClient.reset_notifications properties (nibe_api.py)
# ---------------------------------------------------------------------------


class TestNibeApiResetNotificationsProperties(unittest.TestCase):
    """Hypothesis properties for NibeApiClient.reset_notifications."""

    def _client(self):
        from nibe_api import NibeApiClient
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return NibeApiClient('user:pass', 'https://host:8443', ctx)

    def test_success_returns_true(self):
        """Successful DELETE returns True."""
        client = self._client()
        mock = MagicMock()
        mock.__enter__ = lambda s: s
        mock.__exit__ = MagicMock(return_value=False)
        with patch('urllib.request.urlopen', return_value=mock):
            result = client.reset_notifications()
        self.assertTrue(result)

    @given(st.integers(min_value=400, max_value=599))
    def test_http_error_returns_false(self, code):
        """Any HTTP error returns False — including 405 (not supported)."""
        import urllib.error
        client = self._client()
        err = urllib.error.HTTPError('url', code, 'Error', {}, None)
        with patch('urllib.request.urlopen', side_effect=err):
            result = client.reset_notifications()
        self.assertFalse(result)

    def test_url_error_returns_false(self):
        """Network errors return False."""
        import urllib.error
        client = self._client()
        with patch('urllib.request.urlopen',
                   side_effect=urllib.error.URLError('Connection refused')):
            result = client.reset_notifications()
        self.assertFalse(result)

    def test_always_returns_bool(self):
        """reset_notifications always returns a bool."""
        client = self._client()
        mock = MagicMock()
        mock.__enter__ = lambda s: s
        mock.__exit__ = MagicMock(return_value=False)
        with patch('urllib.request.urlopen', return_value=mock):
            result = client.reset_notifications()
        self.assertIsInstance(result, bool)

    def test_uses_delete_method(self):
        """The request must always use DELETE method."""
        client = self._client()
        captured = []

        def fake_urlopen(req, **kw):
            captured.append(req.get_method())
            mock = MagicMock()
            mock.__enter__ = lambda s: s
            mock.__exit__ = MagicMock(return_value=False)
            return mock

        with patch('urllib.request.urlopen', side_effect=fake_urlopen):
            client.reset_notifications()

        self.assertTrue(captured)
        self.assertEqual(captured[0], 'DELETE')

    def test_url_contains_notifications(self):
        """The URL must always contain '/notifications'."""
        client = self._client()
        captured = []

        def fake_urlopen(req, **kw):
            captured.append(req.full_url)
            mock = MagicMock()
            mock.__enter__ = lambda s: s
            mock.__exit__ = MagicMock(return_value=False)
            return mock

        with patch('urllib.request.urlopen', side_effect=fake_urlopen):
            client.reset_notifications()

        self.assertTrue(captured)
        self.assertIn('notifications', captured[0])


# ---------------------------------------------------------------------------
# NibeApiClient.write_point degenerate range properties (nibe_api.py)
# ---------------------------------------------------------------------------


class TestNibeApiWritePointDegenerateProperties(unittest.TestCase):
    """Hypothesis properties for write_point with degenerate range.

    Degenerate range (min==max) bypasses the min/max validation check —
    this is an intentional design decision documented as a known limitation.
    """

    def _client(self):
        from nibe_api import NibeApiClient
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return NibeApiClient('user:pass', 'https://host:8443', ctx)

    def _degenerate_entity_info(self, val=5):
        return {
            'is_writable': True,
            'metadata': {
                'minValue': val, 'maxValue': val,
                'variableId': 100, 'isWritable': True,
                'modbusRegisterType': 'MODBUS_HOLDING_REGISTER',
            },
            'is_degenerate_range': True,
        }

    @given(st.integers(min_value=-32768, max_value=32767))
    def test_degenerate_range_bypasses_min_max_check(self, value):
        """Any value bypasses min/max validation for degenerate range points."""
        import json as _json
        client = self._client()
        resp = MagicMock()
        resp.read.return_value = _json.dumps({'100': 'modified'}).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        with patch('urllib.request.urlopen', return_value=resp):
            result = client.write_point(100, value, self._degenerate_entity_info())
        # With degenerate range, value is never rejected by range check
        # (may still fail for other reasons, but not range validation)
        self.assertIsInstance(result, bool)

    @given(st.integers(min_value=-32768, max_value=32767))
    def test_degenerate_calls_urlopen(self, value):
        """Degenerate range always proceeds to the network call."""
        client = self._client()
        import json as _json
        resp = MagicMock()
        resp.read.return_value = _json.dumps({'100': 'modified'}).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        with patch('urllib.request.urlopen', return_value=resp) as mock_open:
            client.write_point(100, value, self._degenerate_entity_info())
        mock_open.assert_called_once()

    @given(st.integers(min_value=0, max_value=100))
    def test_non_degenerate_out_of_range_never_calls_urlopen(self, excess):
        """Non-degenerate range rejects out-of-range values without network call."""
        client = self._client()
        ei = {
            'is_writable': True,
            'metadata': {'minValue': 0, 'maxValue': 100,
                         'variableId': 100, 'isWritable': True,
                         'modbusRegisterType': 'MODBUS_HOLDING_REGISTER'},
            'is_degenerate_range': False,
        }
        with patch('urllib.request.urlopen') as mock_open:
            client.write_point(100, 100 + excess + 1, ei)
        mock_open.assert_not_called()


# ===========================================================================
# 23. load_config — configuration resolution
# ===========================================================================


class TestNibeApiRemainingPaths(unittest.TestCase):
    """nibe_api.py: Content-Type header, fetch_bulk_points, fetch_point
    non-404 re-raise, unexpected write response."""

    def setUp(self):
        import ssl
        from nibe_api import NibeApiClient
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        self.client = NibeApiClient(
            "https://192.0.2.1:8443/api/v1/devices/0",
            "Basic dGVzdA==", ctx,
        )

    def _ok(self, body):
        r = MagicMock()
        r.read.return_value = json.dumps(body).encode()
        return r

    def _http_error(self, code):
        import urllib.error
        return urllib.error.HTTPError(
            url='', code=code, msg='err', hdrs=None, fp=None)

    def test_request_with_data_sets_content_type_header(self):
        """When data is passed to request(), Content-Type header is added."""
        captured = {}
        def fake_urlopen(req, _context=None, _timeout=None, **_kw):
            captured['headers'] = dict(req.headers)
            return self._ok({})
        with patch('urllib.request.urlopen', side_effect=fake_urlopen):
            self.client.request(
                "https://192.0.2.1:8443/api/v1/devices/0/points",
                method='PATCH',
                data='{"test": 1}',
            )
        self.assertIn('Content-type', captured['headers'])

    def test_fetch_bulk_points_returns_dict(self):
        """fetch_bulk_points delegates to request() and returns the parsed dict."""
        with patch('urllib.request.urlopen', return_value=self._ok({'100': {}})):
            result = self.client.fetch_bulk_points()
        self.assertEqual(result, {'100': {}})

    def test_write_point_unexpected_response_logs_and_returns_false(self):
        """An unexpected string response (not 'modified', not an error key)
        hits the else branch and returns False."""
        entity_info = {
            'is_writable': True,
            'is_degenerate_range': True,   # skip range checks
            'metadata': {},
        }
        with patch('urllib.request.urlopen',
                   return_value=self._ok({'999': 'some_unexpected_value'})):
            result = self.client.write_point(999, 1, entity_info)
        self.assertFalse(result)




# ===========================================================================
# Phase 2 mutmut survivor tests — nibe_api.py genuine logic gaps
# ===========================================================================


class TestRetryDelayLowerBound(unittest.TestCase):
    """_retry_delay: lower bound is 0 not 1.

    random.uniform(0, cap) — changing 0 to 1 would mean the minimum delay
    is always 1s instead of potentially 0s (full jitter: 0 is valid).
    """

    def test_retry_delay_can_return_zero_or_near_zero(self):
        """Lower bound is 0 — mock random.uniform to verify it's called with 0."""
        from nibe_api import _retry_delay
        with patch('nibe_api.random.uniform', return_value=0.0) as mock_uniform:
            result = _retry_delay()
        # First arg must be 0 (not 1)
        args = mock_uniform.call_args[0]
        self.assertEqual(args[0], 0, f"Lower bound must be 0, got {args[0]}")
        self.assertEqual(result, 0.0)

    def test_retry_delay_upper_bound_is_min_of_base_and_max(self):
        """Upper bound is min(_RETRY_BASE_S, _RETRY_MAX_S) — cap prevents runaway."""
        from nibe_api import _retry_delay, _RETRY_BASE_S, _RETRY_MAX_S
        with patch('nibe_api.random.uniform', return_value=1.0) as mock_uniform:
            _retry_delay()
        args = mock_uniform.call_args[0]
        self.assertEqual(args[1], min(_RETRY_BASE_S, _RETRY_MAX_S))


class TestRequestSslContextAndBody(unittest.TestCase):
    """request(): ssl_context and data=body must be passed to urlopen.

    mutmut survivors: ssl_context dropped (mutmut_38), data=body dropped (mutmut_25).
    Without ssl_context the request would fail against self-signed certs.
    Without data= the body is never sent on PATCH/POST requests.
    """

    def setUp(self):
        import ssl
        self.client = MagicMock()
        self.client.base_url = 'https://192.0.2.1:8443/api/v1/devices/0'
        self.client.auth = 'Basic dXNlcjpwYXNz'
        self.client.ssl_context = ssl.create_default_context()
        # Import the real request method
        from nibe_api import NibeApiClient
        self.client = NibeApiClient.__new__(NibeApiClient)
        self.client.base_url = 'https://192.0.2.1:8443/api/v1/devices/0'
        self.client.auth = 'Basic dXNlcjpwYXNz'
        self.client.ssl_context = MagicMock()

    def test_request_passes_ssl_context_to_urlopen(self):
        """ssl_context must be passed as context= to urllib.request.urlopen."""
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"ok": true}'
        with patch('urllib.request.urlopen', return_value=mock_response) as mock_open:
            self.client.request('https://192.0.2.1:8443/test')
        call_kwargs = mock_open.call_args
        # context= must be passed and must be our ssl_context
        ctx = call_kwargs[1].get('context') or (call_kwargs[0][1] if len(call_kwargs[0]) > 1 else None)
        self.assertIsNotNone(ctx, "ssl_context must be passed to urlopen")
        self.assertEqual(ctx, self.client.ssl_context)

    def test_request_with_data_passes_body_to_request_object(self):
        """data=body must be included in the Request — without it PATCH sends no body."""
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"ok": true}'
        captured_requests = []
        def capture_urlopen(req, **kwargs):
            captured_requests.append(req)
            return mock_response
        with patch('urllib.request.urlopen', side_effect=capture_urlopen):
            self.client.request('https://192.0.2.1:8443/test', data='{"x": 1}')
        self.assertTrue(captured_requests)
        req = captured_requests[0]
        self.assertIsNotNone(req.data, "data= must be set on the Request when data is provided")
        self.assertEqual(req.data, b'{"x": 1}')

# ===========================================================================
# Phase 2 round 2 — nibe_api.py genuine logic gaps
# ===========================================================================


class TestRequestRetryCountAndMethod(unittest.TestCase):
    """request(): exactly 2 attempts (range(2) not range(3)), method arg preserved.

    mutmut_29: range(2) → range(3) — 3 attempts instead of 2.
    mutmut_27: method= dropped from Request() constructor.
    """

    def setUp(self):
        from nibe_api import NibeApiClient
        self.client = NibeApiClient.__new__(NibeApiClient)
        self.client.base_url = 'https://192.0.2.1:8443/api/v1/devices/0'
        self.client.auth = 'Basic dXNlcjpwYXNz'
        self.client.ssl_context = MagicMock()

    def test_exactly_two_attempts_on_transient_error(self):
        """range(2) → exactly 2 urlopen calls on transient failure, not 3."""
        import urllib.error
        error = urllib.error.HTTPError('url', 500, 'Server Error', {}, None)
        with patch('urllib.request.urlopen', side_effect=error) as mock_open, \
             patch('nibe_api.time.sleep'):
            result = self.client.request('https://192.0.2.1:8443/test')
        self.assertIsNone(result)
        self.assertEqual(mock_open.call_count, 2,
                         f"Expected exactly 2 attempts, got {mock_open.call_count}")

    def test_request_method_passed_to_request_object(self):
        """method= must be passed to Request — without it PATCH becomes GET."""
        mock_response = MagicMock()
        mock_response.read.return_value = b'{}'
        captured = []
        def capture(req, **kwargs):
            captured.append(req)
            return mock_response
        with patch('urllib.request.urlopen', side_effect=capture):
            self.client.request('https://192.0.2.1:8443/test', method='PATCH')
        self.assertTrue(captured)
        self.assertEqual(captured[0].get_method(), 'PATCH')


class TestWritePointIsWritableDefault(unittest.TestCase):
    """write_point: is_writable default False not True.

    mutmut_15: default True → non-writable points bypass the writable check.
    mutmut_34: is_degenerate_range default True → non-degenerate ranges skip bounds.
    """

    def setUp(self):
        from nibe_api import NibeApiClient
        self.client = NibeApiClient.__new__(NibeApiClient)
        self.client.base_url = 'https://192.0.2.1:8443'
        self.client.auth = 'Basic x'
        self.client.ssl_context = MagicMock()

    def _entity_info(self, **kwargs):
        defaults = {'is_writable': False, 'metadata': {}}
        defaults.update(kwargs)
        return defaults

    def test_non_writable_point_returns_false_without_http(self):
        """is_writable default False: absent key → not writable → returns False."""
        entity_info = {}  # no 'is_writable' key → defaults to False
        with patch('urllib.request.urlopen') as mock_open:
            result = self.client.write_point(100, 50, entity_info)
        self.assertFalse(result)
        mock_open.assert_not_called()

    def test_degenerate_range_default_false_applies_range_check(self):
        """is_degenerate_range default False: absent key → check bounds.
        With default True: bounds check bypassed for all non-degenerate points."""
        entity_info = {
            'is_writable': True,
            'metadata': {'minValue': 0, 'maxValue': 10},
            # no 'is_degenerate_range' key → defaults to False → check bounds
        }
        with patch('urllib.request.urlopen') as mock_open:
            result = self.client.write_point(100, 99, entity_info)  # 99 > max=10
        self.assertFalse(result)
        mock_open.assert_not_called()


class TestWritePointErrorStringComparisons(unittest.TestCase):
    """write_point: error string comparisons must be == not !=.

    mutmut_128: == "error: no such param" → != — inverted, always falls to else.
    mutmut_134: == "error: read only value" → != — inverted.
    Both mutations cause wrong return/log path but still return False — however
    the behavior is distinguishable if we check the specific code path taken.
    The == vs != is critical: != means the match never fires, all error strings
    fall to the unexpected-response else branch.
    """

    def setUp(self):
        from nibe_api import NibeApiClient
        self.client = NibeApiClient.__new__(NibeApiClient)
        self.client.base_url = 'https://192.0.2.1:8443'
        self.client.auth = 'Basic x'
        self.client.ssl_context = MagicMock()

    def _entity(self):
        return {'is_writable': True, 'is_degenerate_range': False,
                'metadata': {'minValue': 0, 'maxValue': 100}}

    def _mock_response(self, point_id, response_value):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({str(point_id): response_value}).encode()
        return mock_resp

    def test_no_such_param_returns_false(self):
        """'error: no such param' → returns False (not True from else branch)."""
        with patch('urllib.request.urlopen',
                   return_value=self._mock_response(100, 'error: no such param')):
            result = self.client.write_point(100, 50, self._entity())
        self.assertFalse(result)

    def test_read_only_value_returns_false(self):
        """'error: read only value' → returns False."""
        with patch('urllib.request.urlopen',
                   return_value=self._mock_response(100, 'error: read only value')):
            result = self.client.write_point(100, 50, self._entity())
        self.assertFalse(result)

    def test_modified_response_returns_true(self):
        """'modified' → returns True (unchanged by these mutations)."""
        with patch('urllib.request.urlopen',
                   return_value=self._mock_response(100, 'modified')):
            result = self.client.write_point(100, 50, self._entity())
        self.assertTrue(result)


# ===========================================================================
# Phase 2 round 3 — nibe_api.py remaining genuine logic gaps
# ===========================================================================


class TestRequestAuthAndNotFoundBehaviour(unittest.TestCase):
    """request(): 401/403 raise immediately (no retry); 404 raises; others return None.

    mutmut survivors:
    - e.code in (401, 403) membership: mutation to (401, 404) makes 403 fall through to retry.
    - e.code == 404 check: mutation to != makes 404 fall through to retry path then return None.
    Both are critical: auth failures and missing resources must propagate to callers.
    """

    def setUp(self):
        from nibe_api import NibeApiClient
        self.client = NibeApiClient.__new__(NibeApiClient)
        self.client.base_url = 'https://192.0.2.1:8443/api/v1/devices/0'
        self.client.auth = 'Basic dXNlcjpwYXNz'
        self.client.ssl_context = MagicMock()

    def _http_error(self, code):
        import urllib.error
        return urllib.error.HTTPError('url', code, f'HTTP {code}', {}, None)

    def test_401_raises_immediately_no_retry(self):
        """HTTP 401 must raise HTTPError — not retry, not return None."""
        import urllib.error
        with patch('urllib.request.urlopen', side_effect=self._http_error(401)) as mock_open:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.client.request('https://192.0.2.1:8443/test')
        self.assertEqual(ctx.exception.code, 401)
        self.assertEqual(mock_open.call_count, 1, "401 must not retry")

    def test_403_raises_immediately_no_retry(self):
        """HTTP 403 must raise HTTPError — not retry, not return None.

        The membership check is (401, 403) not (401, 404): a mutation replacing
        403 with 404 would make 403 fall through to the retry path.
        """
        import urllib.error
        with patch('urllib.request.urlopen', side_effect=self._http_error(403)) as mock_open:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.client.request('https://192.0.2.1:8443/test')
        self.assertEqual(ctx.exception.code, 403)
        self.assertEqual(mock_open.call_count, 1, "403 must not retry")

    def test_404_raises_not_retried_not_none(self):
        """HTTP 404 must raise HTTPError — not be swallowed and returned as None."""
        import urllib.error
        with patch('urllib.request.urlopen', side_effect=self._http_error(404)) as mock_open:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.client.request('https://192.0.2.1:8443/test')
        self.assertEqual(ctx.exception.code, 404)
        self.assertEqual(mock_open.call_count, 1, "404 must not retry")

    def test_500_returns_none_after_two_attempts(self):
        """HTTP 500 is transient: retried once then returns None (not raises)."""
        with patch('urllib.request.urlopen', side_effect=self._http_error(500)) as mock_open, \
             patch('nibe_api.time.sleep'):
            result = self.client.request('https://192.0.2.1:8443/test')
        self.assertIsNone(result)
        self.assertEqual(mock_open.call_count, 2)

    def test_404_distinct_from_403_in_handling(self):
        """403 raises (auth); 404 raises (missing resource). Both must raise,
        but this confirms the (401, 403) membership does not accidentally absorb 404."""
        import urllib.error
        for code in (401, 403, 404):
            with self.subTest(code=code):
                with patch('urllib.request.urlopen', side_effect=self._http_error(code)):
                    with self.assertRaises(urllib.error.HTTPError):
                        self.client.request('https://192.0.2.1:8443/test')


class TestWritePointResponseKeyParsing(unittest.TestCase):
    """write_point: exact key names in PATCH response parsing.

    mutmut survivors:
    - point_resp == "modified": mutation == → != makes success never returned.
    - dv = point_resp.get('value', {}): 'value' → 'VALUE' means isOk never found.
    - dv.get('isOk'): 'isOk' → 'isok' means full-object response always returns False.
    - isinstance(point_resp, dict): dict → list means full-object path never taken.
    """

    def setUp(self):
        from nibe_api import NibeApiClient
        self.client = NibeApiClient.__new__(NibeApiClient)
        self.client.base_url = 'https://192.0.2.1:8443'
        self.client.auth = 'Basic x'
        self.client.ssl_context = MagicMock()

    def _entity(self, pid=42):
        return {'is_writable': True, 'is_degenerate_range': False,
                'metadata': {'minValue': 0, 'maxValue': 100}}

    def _resp(self, body):
        m = MagicMock()
        m.read.return_value = json.dumps(body).encode()
        return m

    def test_modified_string_returns_true(self):
        """point_resp == 'modified' → True. Mutation == → != makes this False."""
        with patch('urllib.request.urlopen', return_value=self._resp({"42": "modified"})):
            self.assertTrue(self.client.write_point(42, 1, self._entity()))

    def test_non_modified_string_returns_false(self):
        """point_resp != 'modified' (e.g. 'MODIFIED') → False.
        Confirms the equality is case-sensitive and mutation would break it."""
        with patch('urllib.request.urlopen', return_value=self._resp({"42": "MODIFIED"})):
            self.assertFalse(self.client.write_point(42, 1, self._entity()))

    def test_full_object_value_key_lowercase(self):
        """Full-object response uses lowercase 'value' key, not 'VALUE'.
        dv = point_resp.get('value', {}) — mutation 'value' → 'VALUE' gives {}
        then dv.get('isOk') → None → returns False."""
        body = {"42": {"value": {"isOk": True}}}
        with patch('urllib.request.urlopen', return_value=self._resp(body)):
            self.assertTrue(self.client.write_point(42, 1, self._entity()))

    def test_full_object_isok_key_exact_case(self):
        """isOk key is case-sensitive: 'isOk' True → returns True.
        Mutation 'isOk' → 'isok' → get returns None → falsy → returns False."""
        body = {"42": {"value": {"isOk": True}}}
        with patch('urllib.request.urlopen', return_value=self._resp(body)):
            self.assertTrue(self.client.write_point(42, 1, self._entity()))

    def test_full_object_isok_false_returns_false(self):
        """isOk=False → returns False even though response is a dict."""
        body = {"42": {"value": {"isOk": False}}}
        with patch('urllib.request.urlopen', return_value=self._resp(body)):
            self.assertFalse(self.client.write_point(42, 1, self._entity()))

    def test_list_response_not_treated_as_dict(self):
        """isinstance(point_resp, dict): a list response must not be treated as dict.
        Mutation dict → list would make a list trigger the dict path."""
        body = {"42": ["modified"]}   # list, not dict
        with patch('urllib.request.urlopen', return_value=self._resp(body)):
            self.assertFalse(self.client.write_point(42, 1, self._entity()))

    def test_point_id_string_key_used_for_lookup(self):
        """Response key is str(point_id): response {"42": "modified"} for pid=42.
        If key were int(point_id) the lookup would always miss."""
        body = {"42": "modified"}
        with patch('urllib.request.urlopen', return_value=self._resp(body)):
            self.assertTrue(self.client.write_point(42, 1, self._entity()))

    def test_wrong_point_id_key_returns_false(self):
        """Response key for different pid → point_resp is None → falls through to False."""
        body = {"99": "modified"}   # pid=42 not in response
        with patch('urllib.request.urlopen', return_value=self._resp(body)):
            self.assertFalse(self.client.write_point(42, 1, self._entity()))


class TestWritePointHttpMethod(unittest.TestCase):
    """write_point sends PATCH not GET/POST; reset_notifications sends DELETE.

    mutmut survivors: method='PATCH' → method='GET' (or dropped).
    Wrong method = firmware rejects or routes incorrectly.
    """

    def setUp(self):
        from nibe_api import NibeApiClient
        self.client = NibeApiClient.__new__(NibeApiClient)
        self.client.base_url = 'https://192.0.2.1:8443'
        self.client.auth = 'Basic x'
        self.client.ssl_context = MagicMock()

    def _entity(self):
        return {'is_writable': True, 'is_degenerate_range': False,
                'metadata': {'minValue': 0, 'maxValue': 100}}

    def test_write_point_uses_patch_method(self):
        """write_point must use HTTP PATCH, not GET or POST."""
        captured = []
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"42": "modified"}).encode()
        def capture(req, **kwargs):
            captured.append(req)
            return mock_resp
        with patch('urllib.request.urlopen', side_effect=capture):
            self.client.write_point(42, 1, self._entity())
        self.assertTrue(captured)
        self.assertEqual(captured[0].get_method(), 'PATCH')

    def test_reset_notifications_uses_delete_method(self):
        """reset_notifications must use HTTP DELETE."""
        captured = []
        mock_resp = MagicMock()
        mock_resp.read.return_value = b''
        def capture(req, **kwargs):
            captured.append(req)
            return mock_resp
        with patch('urllib.request.urlopen', side_effect=capture):
            self.client.reset_notifications()
        self.assertTrue(captured)
        self.assertEqual(captured[0].get_method(), 'DELETE')

    def test_write_device_mode_uses_post_method(self):
        """write_device_mode must use HTTP POST."""
        captured = []
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{}'
        def capture(req, **kwargs):
            captured.append(req)
            return mock_resp
        with patch('urllib.request.urlopen', side_effect=capture):
            self.client.write_device_mode('smartmode', 'away')
        self.assertTrue(captured)
        self.assertEqual(captured[0].get_method(), 'POST')


class TestFetchPointNotFoundHandling(unittest.TestCase):
    """fetch_point: HTTP 404 → None; non-404 HTTPError → re-raises.

    mutmut survivors:
    - e.code == 404: mutation to != makes 404 re-raise instead of return None.
    - raise after non-404: mutation drops raise, falls through to return result=None silently.
    """

    def setUp(self):
        from nibe_api import NibeApiClient
        self.client = NibeApiClient.__new__(NibeApiClient)
        self.client.base_url = 'https://192.0.2.1:8443'
        self.client.auth = 'Basic x'
        self.client.ssl_context = MagicMock()

    def _http_error(self, code):
        import urllib.error
        return urllib.error.HTTPError('url', code, f'HTTP {code}', {}, None)

    def test_404_returns_none(self):
        """fetch_point HTTP 404 → returns None (dynamic point inactive)."""
        with patch('urllib.request.urlopen', side_effect=self._http_error(404)):
            result = self.client.fetch_point(99)
        self.assertIsNone(result)

    def test_non_404_http_error_reraises(self):
        """fetch_point re-raises HTTPErrors that request() propagates (401, 403).
        request() absorbs 500 and returns None; fetch_point only sees 401/403/404.
        The critical mutation is e.code == 404 → != : 404 would re-raise instead
        of returning None, and non-404 auth errors that reach fetch_point re-raise."""
        import urllib.error
        for code in (401, 403):
            with self.subTest(code=code):
                with patch('urllib.request.urlopen', side_effect=self._http_error(code)):
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        self.client.fetch_point(99)
                self.assertEqual(ctx.exception.code, code)

    def test_success_returns_dict(self):
        """fetch_point success → returns parsed JSON dict."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"variableId": 99, "value": 42}'
        with patch('urllib.request.urlopen', return_value=mock_resp):
            result = self.client.fetch_point(99)
        self.assertIsNotNone(result)
        self.assertEqual(result['variableId'], 99)


class TestWriteDeviceModePayloadAndUrl(unittest.TestCase):
    """write_device_mode: URL includes mode_type; payload key matches mode_type.

    mutmut survivors in write_device_mode (21 total):
    - URL construction: f"{base_url}/{mode_type}" — mode_type dropped = wrong URL.
    - Payload key: {mode_type: value} — key is mode_type not a fixed string.
    - method='POST': mutation to 'GET' = wrong method.
    - return True on success: mutation to False.
    """

    def setUp(self):
        from nibe_api import NibeApiClient
        self.client = NibeApiClient.__new__(NibeApiClient)
        self.client.base_url = 'https://192.0.2.1:8443'
        self.client.auth = 'Basic x'
        self.client.ssl_context = MagicMock()

    def test_url_includes_mode_type(self):
        """URL must be {base_url}/{mode_type} — mode_type must appear in URL."""
        captured = []
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{}'
        def capture(req, **kwargs):
            captured.append(req)
            return mock_resp
        with patch('urllib.request.urlopen', side_effect=capture):
            self.client.write_device_mode('smartmode', 'away')
        self.assertTrue(captured)
        self.assertIn('smartmode', captured[0].full_url)

    def test_payload_key_is_mode_type(self):
        """Payload must be JSON with mode_type as the key: {"smartmode": "away"}."""
        captured = []
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{}'
        def capture(req, **kwargs):
            captured.append(req)
            return mock_resp
        with patch('urllib.request.urlopen', side_effect=capture):
            self.client.write_device_mode('smartmode', 'away')
        self.assertTrue(captured)
        body = json.loads(captured[0].data.decode())
        self.assertIn('smartmode', body)
        self.assertEqual(body['smartmode'], 'away')

    def test_aidmode_payload_key(self):
        """aidmode uses 'aidmode' as payload key with 'on'/'off' values."""
        captured = []
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{}'
        def capture(req, **kwargs):
            captured.append(req)
            return mock_resp
        with patch('urllib.request.urlopen', side_effect=capture):
            self.client.write_device_mode('aidmode', 'on')
        body = json.loads(captured[0].data.decode())
        self.assertIn('aidmode', body)
        self.assertEqual(body['aidmode'], 'on')

    def test_success_returns_true(self):
        """Successful POST → returns True."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{}'
        with patch('urllib.request.urlopen', return_value=mock_resp):
            self.assertTrue(self.client.write_device_mode('smartmode', 'normal'))
