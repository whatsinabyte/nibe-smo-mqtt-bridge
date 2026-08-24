"""Tests for nibe_connectivity_check.py — the independent ping+curl
diagnostic behind the "Test API Connection" debug management button."""

import subprocess
import unittest
from unittest.mock import MagicMock, patch

from hypothesis import example, given
from hypothesis import strategies as st


class TestRunPing(unittest.TestCase):
    def test_success_returncode_0(self):
        from nibe_connectivity_check import _run_ping
        with patch('subprocess.run', return_value=MagicMock(returncode=0, stdout='', stderr='')):
            result = _run_ping('192.0.2.1')
        self.assertTrue(result['ok'])
        self.assertIn('192.0.2.1', result['summary'])

    def test_no_reply_returncode_1(self):
        from nibe_connectivity_check import _run_ping
        with patch('subprocess.run', return_value=MagicMock(returncode=1, stdout='', stderr='')):
            result = _run_ping('192.0.2.1')
        self.assertFalse(result['ok'])
        self.assertIn('does not respond', result['summary'])

    def test_other_returncode_includes_stderr_detail(self):
        from nibe_connectivity_check import _run_ping
        with patch('subprocess.run', return_value=MagicMock(
                returncode=2, stdout='', stderr='ping: bad address')):
            result = _run_ping('not-a-host')
        self.assertFalse(result['ok'])
        self.assertIn('bad address', result['summary'])

    def test_ping_not_installed(self):
        from nibe_connectivity_check import _run_ping
        with patch('subprocess.run', side_effect=FileNotFoundError):
            result = _run_ping('192.0.2.1')
        self.assertFalse(result['ok'])
        self.assertIn('not installed', result['summary'])

    def test_ping_timeout_expired(self):
        from nibe_connectivity_check import _run_ping
        with patch('subprocess.run',
                   side_effect=subprocess.TimeoutExpired(cmd='ping', timeout=20)):
            result = _run_ping('192.0.2.1')
        self.assertFalse(result['ok'])
        self.assertIn('did not exit', result['summary'])

    def test_uses_count_and_host_in_command(self):
        """The actual ping invocation must target the real host and use
        the count/timeout parameters — not hardcoded/dropped args."""
        from nibe_connectivity_check import _run_ping
        with patch('subprocess.run', return_value=MagicMock(returncode=0, stdout='', stderr='')) as mock_run:
            _run_ping('192.0.2.9', count=5, timeout=3)
        cmd = mock_run.call_args.args[0]
        self.assertEqual(cmd, ['ping', '-c', '5', '-W', '3', '192.0.2.9'])

    def test_default_count_and_timeout(self):
        """Pins the default count=3/timeout=5 signature — a mutant that
        changes either default would silently alter both the ping command
        and the subprocess timeout for every caller that doesn't override
        them."""
        from nibe_connectivity_check import _run_ping
        with patch('subprocess.run', return_value=MagicMock(returncode=0, stdout='', stderr='')) as mock_run:
            _run_ping('192.0.2.9')
        self.assertEqual(mock_run.call_args.args[0], ['ping', '-c', '3', '-W', '5', '192.0.2.9'])
        self.assertEqual(mock_run.call_args.kwargs['timeout'], 20)

    def test_subprocess_run_invoked_with_exact_kwargs(self):
        from nibe_connectivity_check import _run_ping
        with patch('subprocess.run', return_value=MagicMock(returncode=0, stdout='', stderr='')) as mock_run:
            _run_ping('192.0.2.9', count=5, timeout=3)
        self.assertEqual(mock_run.call_args.kwargs, {
            'capture_output': True,
            'text': True,
            'timeout': 20,
            'check': False,
        })

    def test_ping_not_installed_exact_dict(self):
        from nibe_connectivity_check import _run_ping
        with patch('subprocess.run', side_effect=FileNotFoundError):
            result = _run_ping('192.0.2.1')
        self.assertEqual(result, {
            'ok': False,
            'summary': 'ping is not installed in this container.',
        })

    def test_ping_timeout_expired_exact_dict(self):
        from nibe_connectivity_check import _run_ping
        with patch('subprocess.run',
                   side_effect=subprocess.TimeoutExpired(cmd='ping', timeout=20)):
            result = _run_ping('192.0.2.1')
        self.assertEqual(result, {
            'ok': False,
            'summary': 'ping did not exit within the expected time.',
        })

    def test_other_returncode_with_no_detail_ends_in_bare_period(self):
        """When ping produces no stderr/stdout detail on an unexpected
        return code, the summary must end in a bare '.' — not silently
        drop the period."""
        from nibe_connectivity_check import _run_ping
        with patch('subprocess.run', return_value=MagicMock(returncode=2, stdout='', stderr='')):
            result = _run_ping('not-a-host')
        self.assertEqual(result['summary'], 'Could not ping not-a-host.')


class TestRunPingProperties(unittest.TestCase):
    """Hypothesis properties for _run_ping: the cmd construction and the
    subprocess timeout budget must hold for any count/timeout, not just
    the specific values the example-based tests happen to use."""

    @given(count=st.integers(min_value=1, max_value=20),
           timeout=st.integers(min_value=1, max_value=60))
    @example(count=5, timeout=3)   # the exact values test_uses_count_and_host_in_command pins
    @example(count=3, timeout=5)   # the real default values
    def test_cmd_reflects_real_count_and_timeout(self, count, timeout):
        from nibe_connectivity_check import _run_ping
        with patch('subprocess.run',
                   return_value=MagicMock(returncode=0, stdout='', stderr='')) as mock_run:
            _run_ping('192.0.2.9', count=count, timeout=timeout)
        cmd = mock_run.call_args.args[0]
        self.assertEqual(cmd, ['ping', '-c', str(count), '-W', str(timeout), '192.0.2.9'])

    @given(count=st.integers(min_value=1, max_value=20),
           timeout=st.integers(min_value=1, max_value=60))
    @example(count=5, timeout=3)
    @example(count=3, timeout=5)
    def test_subprocess_timeout_is_timeout_times_count_plus_five(self, count, timeout):
        from nibe_connectivity_check import _run_ping
        with patch('subprocess.run',
                   return_value=MagicMock(returncode=0, stdout='', stderr='')) as mock_run:
            _run_ping('192.0.2.9', count=count, timeout=timeout)
        self.assertEqual(mock_run.call_args.kwargs['timeout'], timeout * count + 5)


class TestRunCurl(unittest.TestCase):
    def _curl_result(self, returncode=0, stdout='', stderr=''):
        return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)

    def test_success_extracts_http_code(self):
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(stdout='HTTP_CODE:401')):
            result = _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None)
        self.assertTrue(result['ok'])
        self.assertEqual(result['http_code'], 401)
        self.assertIn('401', result['summary'])

    def test_no_ca_cert_uses_insecure_flag(self):
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(stdout='HTTP_CODE:200')) as mock_run:
            _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None)
        cmd = mock_run.call_args.args[0]
        self.assertIn('-k', cmd)
        self.assertNotIn('--cacert', cmd)

    def test_no_ca_cert_uses_shared_tls_compat_cipher_constant(self):
        """Regression: this diagnostic's cipher-compatibility widening must
        stay in lockstep with _build_ssl_context's (app/generate_nibe_mqtt.py)
        — both import TLS_COMPAT_CIPHERS from nibe_utils rather than each
        hardcoding their own literal, so a future change to one can't
        silently drift from the other and make this diagnostic report a
        false negative against a controller the real polling connection can
        actually reach."""
        from nibe_connectivity_check import _run_curl
        from nibe_utils import TLS_COMPAT_CIPHERS
        with patch('subprocess.run', return_value=self._curl_result(stdout='HTTP_CODE:200')) as mock_run:
            _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None)
        cmd = mock_run.call_args.args[0]
        self.assertIn(TLS_COMPAT_CIPHERS, cmd)

    def test_ca_cert_path_uses_cacert_flag_not_insecure(self):
        """When a CA cert is configured, the check must verify against it
        (matching what NibeApiClient's own ssl_context actually does) —
        not silently fall back to -k, which would give a falsely
        reassuring result for a user relying on verified TLS."""
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(stdout='HTTP_CODE:200')) as mock_run:
            _run_curl('https://192.0.2.1:8443/api/v1/devices/0', '/ssl/nibe-ca.pem')
        cmd = mock_run.call_args.args[0]
        self.assertIn('--cacert', cmd)
        self.assertIn('/ssl/nibe-ca.pem', cmd)
        self.assertNotIn('-k', cmd)

    def test_exact_cmd_no_ca_no_auth(self):
        """Pin the full curl command line for the no-CA, no-auth path — a
        mistyped flag (-w vs -W, -o vs -O), constant (HTTP_CODE:%{http_code}
        vs a mistyped variant), or a `cmd = [...]` overwrite instead of
        `cmd += [...]` in the CA branch could all slip past `assertIn`
        checks elsewhere in this file."""
        from nibe_connectivity_check import _run_curl
        from nibe_utils import TLS_COMPAT_CIPHERS
        with patch('subprocess.run', return_value=self._curl_result(stdout='HTTP_CODE:200')) as mock_run:
            _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None, timeout=10)
        cmd = mock_run.call_args.args[0]
        self.assertEqual(cmd, [
            'curl', '-sS', '--max-time', '10', '-o', '/dev/null',
            '-w', 'HTTP_CODE:%{http_code}',
            '-k', '--tlsv1.0', '--ciphers', TLS_COMPAT_CIPHERS,
            'https://192.0.2.1:8443/api/v1/devices/0/points',
        ])

    def test_exact_cmd_with_ca_and_auth(self):
        """Pin the full curl command line for the CA-verified, authenticated
        path — the `cmd += ['--cacert', ...]` line must extend the base
        command, not replace it (a `cmd = [...]` mutation would silently
        drop 'curl' itself and every prior flag)."""
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(stdout='HTTP_CODE:200')) as mock_run:
            _run_curl('https://192.0.2.1:8443/api/v1/devices/0', '/ssl/nibe-ca.pem',
                      auth_header='Basic dGVzdA==', timeout=10)
        cmd = mock_run.call_args.args[0]
        self.assertEqual(cmd, [
            'curl', '-sS', '--max-time', '10', '-o', '/dev/null',
            '-w', 'HTTP_CODE:%{http_code}',
            '--cacert', '/ssl/nibe-ca.pem',
            '-H', 'Authorization: Basic dGVzdA==',
            'https://192.0.2.1:8443/api/v1/devices/0/points',
        ])

    def test_url_targets_points_endpoint(self):
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(stdout='HTTP_CODE:200')) as mock_run:
            _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None)
        cmd = mock_run.call_args.args[0]
        self.assertEqual(cmd[-1], 'https://192.0.2.1:8443/api/v1/devices/0/points')

    def test_exit_code_7_connection_refused(self):
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(returncode=7)):
            result = _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None)
        self.assertFalse(result['ok'])
        self.assertIn('Could not connect', result['summary'])

    def test_exit_code_28_timeout(self):
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(returncode=28)):
            result = _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None)
        self.assertFalse(result['ok'])
        self.assertIn('timed out', result['summary'])

    def test_unknown_exit_code_falls_back_to_generic_message(self):
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(returncode=99)):
            result = _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None)
        self.assertFalse(result['ok'])
        self.assertIn('99', result['summary'])

    def test_curl_not_installed(self):
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', side_effect=FileNotFoundError):
            result = _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None)
        self.assertFalse(result['ok'])
        self.assertIn('not installed', result['summary'])

    def test_curl_hangs_past_timeout(self):
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run',
                   side_effect=subprocess.TimeoutExpired(cmd='curl', timeout=15)):
            result = _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None)
        self.assertFalse(result['ok'])
        self.assertIn('hang', result['summary'])

    def test_malformed_http_code_does_not_crash(self):
        """A curl write-out line that fails int() parsing must not raise —
        http_code just stays None."""
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(stdout='HTTP_CODE:garbage')):
            result = _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None)
        self.assertTrue(result['ok'])
        self.assertIsNone(result['http_code'])

    def test_tls_verified_true_when_ca_cert_path_given(self):
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(stdout='HTTP_CODE:200')):
            result = _run_curl('https://192.0.2.1:8443/api/v1/devices/0', '/ssl/nibe-ca.pem')
        self.assertIs(result['tls_verified'], True)

    def test_tls_verified_false_when_no_ca_cert_path(self):
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(stdout='HTTP_CODE:200')):
            result = _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None)
        self.assertIs(result['tls_verified'], False)

    def test_subprocess_run_invoked_with_exact_kwargs(self):
        """Pins the subprocess.run keyword arguments: capture_output/text
        must be True (else stdout/stderr aren't captured as strings),
        timeout must be the curl --max-time plus a 5s grace period (else a
        hung curl process outlives the intended timeout), and check must be
        False (curl's own non-zero exit codes are handled explicitly, not
        raised as CalledProcessError)."""
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(stdout='HTTP_CODE:200')) as mock_run:
            _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None, timeout=10)
        self.assertEqual(mock_run.call_args.kwargs, {
            'capture_output': True,
            'text': True,
            'timeout': 15,
            'check': False,
        })

    def test_default_timeout_is_exactly_ten(self):
        """Pins the default timeout=10 — a wrong default would silently
        change both curl's --max-time and the subprocess timeout for every
        caller that doesn't override it (run_connectivity_check doesn't)."""
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(stdout='HTTP_CODE:200')) as mock_run:
            _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None)
        self.assertIn('10', mock_run.call_args.args[0])
        self.assertEqual(mock_run.call_args.kwargs['timeout'], 15)

    def test_curl_not_installed_summary_exact_text(self):
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', side_effect=FileNotFoundError):
            result = _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None)
        self.assertEqual(result, {
            'ok': False,
            'summary': 'curl is not installed in this container.',
            'http_code': None,
            'tls_verified': False,
        })

    def test_curl_hangs_summary_exact_text_and_keys(self):
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run',
                   side_effect=subprocess.TimeoutExpired(cmd='curl', timeout=15)):
            result = _run_curl('https://192.0.2.1:8443/api/v1/devices/0', '/ssl/nibe-ca.pem')
        self.assertEqual(result, {
            'ok': False,
            'summary': 'curl did not exit within the expected time — treating as a hang.',
            'http_code': None,
            'tls_verified': True,
        })

    def test_verified_note_mentions_ca_when_ca_cert_configured(self):
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(stdout='HTTP_CODE:200')):
            result = _run_curl('https://192.0.2.1:8443/api/v1/devices/0', '/ssl/nibe-ca.pem')
        self.assertIn('(TLS verified against configured CA)', result['summary'])
        self.assertNotIn('self-signed', result['summary'])

    def test_verified_note_mentions_self_signed_when_no_ca_cert(self):
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(stdout='HTTP_CODE:200')):
            result = _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None)
        self.assertIn('(TLS verification skipped — self-signed cert)', result['summary'])
        self.assertNotIn('verified against configured CA', result['summary'])

    def test_nonzero_exit_detail_appended_when_not_duplicate_of_reason(self):
        """The exit-code reason ('Could not connect...') and curl's own
        stderr text are distinct strings here, so stderr must be appended
        in parentheses rather than silently dropped."""
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run',
                   return_value=self._curl_result(returncode=7, stderr='connection refused by peer')):
            result = _run_curl('https://192.0.2.1:8443/api/v1/devices/0', '/ssl/nibe-ca.pem')
        self.assertEqual(result, {
            'ok': False,
            'summary': (
                'Could not connect — device unreachable at this address/port '
                '(network/firewall/VLAN block, or the device is offline). '
                '(connection refused by peer)'
            ),
            'http_code': None,
            'tls_verified': True,
        })

    def test_nonzero_exit_detail_omitted_when_already_part_of_reason(self):
        """If curl's stderr text is already contained in the canned exit-code
        reason, it must not be duplicated in parentheses."""
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run',
                   return_value=self._curl_result(returncode=7, stderr='Could not connect')):
            result = _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None)
        self.assertEqual(
            result['summary'],
            'Could not connect — device unreachable at this address/port '
            '(network/firewall/VLAN block, or the device is offline).',
        )

    def test_http_300_with_auth_is_not_ok(self):
        """The success range is [200, 300) — 300 itself must NOT count as
        success, distinguishing `< 300` from `<= 300` or `< 301`."""
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(stdout='HTTP_CODE:300')):
            result = _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None, 'Basic dGVzdA==')
        self.assertFalse(result['ok'])
        self.assertEqual(result['http_code'], 300)

    def test_reachable_no_auth_exact_dict(self):
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(stdout='HTTP_CODE:200')):
            result = _run_curl('https://192.0.2.1:8443/api/v1/devices/0', '/ssl/nibe-ca.pem')
        self.assertEqual(result, {
            'ok': True,
            'summary': 'Reachable — HTTP 200 from the device (TLS verified against configured CA).',
            'http_code': 200,
            'tls_verified': True,
        })

    def test_reachable_and_authenticated_exact_dict(self):
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(stdout='HTTP_CODE:200')):
            result = _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None, 'Basic dGVzdA==')
        self.assertEqual(result, {
            'ok': True,
            'summary': 'Reachable and authenticated — HTTP 200 (TLS verification skipped — self-signed cert).',
            'http_code': 200,
            'tls_verified': False,
        })

    def test_credentials_rejected_exact_dict(self):
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(stdout='HTTP_CODE:401')):
            result = _run_curl('https://192.0.2.1:8443/api/v1/devices/0', '/ssl/nibe-ca.pem', 'Basic wrong=')
        self.assertEqual(result, {
            'ok': False,
            'summary': (
                'Reachable (TLS verified against configured CA), but credentials were rejected '
                '(HTTP 401) — check nibe_username/nibe_password in add-on options, '
                'or nibe_basic_auth in secrets.yaml.'
            ),
            'http_code': 401,
            'tls_verified': True,
        })

    def test_unexpected_status_with_auth_exact_dict(self):
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(stdout='HTTP_CODE:500')):
            result = _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None, 'Basic dGVzdA==')
        self.assertEqual(result, {
            'ok': False,
            'summary': 'Reachable (TLS verification skipped — self-signed cert), but got an unexpected HTTP 500.',
            'http_code': 500,
            'tls_verified': False,
        })

    # ── auth_header — real-credential mode ──────────────────────────────────

    def test_auth_header_added_to_command(self):
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(stdout='HTTP_CODE:200')) as mock_run:
            _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None, 'Basic dXNlcjpwYXNz')
        cmd = mock_run.call_args.args[0]
        self.assertIn('-H', cmd)
        self.assertIn('Authorization: Basic dXNlcjpwYXNz', cmd)

    def test_no_auth_header_omits_header_flag(self):
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(stdout='HTTP_CODE:200')) as mock_run:
            _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None)
        self.assertNotIn('-H', mock_run.call_args.args[0])

    def test_auth_header_200_is_ok(self):
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(stdout='HTTP_CODE:200')):
            result = _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None, 'Basic dGVzdA==')
        self.assertTrue(result['ok'])
        self.assertIn('authenticated', result['summary'])

    def test_auth_header_401_is_not_ok_and_names_credentials(self):
        """With real credentials supplied, a 401 is a genuine, actionable
        finding — must be reported as a credentials problem, not silently
        treated as 'reachable' the way the no-auth mode does."""
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(stdout='HTTP_CODE:401')):
            result = _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None, 'Basic d3Jvbmc=')
        self.assertFalse(result['ok'])
        self.assertEqual(result['http_code'], 401)
        self.assertIn('credentials were rejected', result['summary'])
        self.assertIn('nibe_username', result['summary'])

    def test_auth_header_403_is_not_ok(self):
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(stdout='HTTP_CODE:403')):
            result = _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None, 'Basic d3Jvbmc=')
        self.assertFalse(result['ok'])
        self.assertIn('credentials were rejected', result['summary'])

    def test_auth_header_unexpected_status_is_not_ok(self):
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(stdout='HTTP_CODE:500')):
            result = _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None, 'Basic dGVzdA==')
        self.assertFalse(result['ok'])
        self.assertIn('unexpected HTTP 500', result['summary'])

    def test_no_auth_header_401_still_counts_as_reachable(self):
        """Pure reachability mode (no credentials passed) must not be
        broken by adding auth-aware behavior — a 401 still just proves
        the network/TLS path works."""
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(stdout='HTTP_CODE:401')):
            result = _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None)
        self.assertTrue(result['ok'])


class TestRunCurlProperties(unittest.TestCase):
    """Hypothesis properties for _run_curl: the --max-time/subprocess
    timeout relationship and the HTTP-code success classification must
    hold for any value, not just the specific ones checked by example."""

    def _curl_result(self, returncode=0, stdout='', stderr=''):
        return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)

    @given(timeout=st.integers(min_value=1, max_value=120))
    @example(timeout=10)   # the real default
    def test_max_time_flag_matches_timeout_argument(self, timeout):
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(stdout='HTTP_CODE:200')) as mock_run:
            _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None, timeout=timeout)
        cmd = mock_run.call_args.args[0]
        idx = cmd.index('--max-time')
        self.assertEqual(cmd[idx + 1], str(timeout))

    @given(timeout=st.integers(min_value=1, max_value=120))
    @example(timeout=10)
    def test_subprocess_timeout_is_max_time_plus_five(self, timeout):
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(stdout='HTTP_CODE:200')) as mock_run:
            _run_curl('https://192.0.2.1:8443/api/v1/devices/0', None, timeout=timeout)
        self.assertEqual(mock_run.call_args.kwargs['timeout'], timeout + 5)

    @given(http_code=st.integers(min_value=200, max_value=299))
    @example(http_code=200)
    def test_any_2xx_with_auth_header_is_ok(self, http_code):
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(stdout=f'HTTP_CODE:{http_code}')):
            result = _run_curl(
                'https://192.0.2.1:8443/api/v1/devices/0', None, 'Basic dGVzdA==',
            )
        self.assertTrue(result['ok'])
        self.assertEqual(result['http_code'], http_code)

    @given(http_code=st.integers(min_value=300, max_value=999).filter(
        lambda c: c not in (401, 403)))
    @example(http_code=300)   # the exact upper-bound-exclusive boundary
    @example(http_code=500)
    def test_non_2xx_non_credential_code_with_auth_header_is_not_ok(self, http_code):
        from nibe_connectivity_check import _run_curl
        with patch('subprocess.run', return_value=self._curl_result(stdout=f'HTTP_CODE:{http_code}')):
            result = _run_curl(
                'https://192.0.2.1:8443/api/v1/devices/0', None, 'Basic dGVzdA==',
            )
        self.assertFalse(result['ok'])
        self.assertEqual(result['http_code'], http_code)


class TestRunConnectivityCheck(unittest.TestCase):
    """run_connectivity_check() combines ping + curl into one overall result."""

    def _patch_both(self, ping_ok, curl_ok):
        return (
            patch('nibe_connectivity_check._run_ping',
                  return_value={'ok': ping_ok, 'summary': 'ping result'}),
            patch('nibe_connectivity_check._run_curl',
                  return_value={'ok': curl_ok, 'summary': 'curl result', 'http_code': 200 if curl_ok else None}),
        )

    def test_start_log_has_exact_text_and_real_host_and_base_url(self):
        from nibe_connectivity_check import run_connectivity_check
        p1, p2 = self._patch_both(True, True)
        with p1, p2, patch('nibe_connectivity_check.log_commands') as mock_log:
            run_connectivity_check('192.0.2.1', 'https://192.0.2.1:8443/api/v1/devices/0')
        mock_log.info.assert_any_call(
            "Running connectivity check against %s (%s)",
            '192.0.2.1', 'https://192.0.2.1:8443/api/v1/devices/0',
        )

    def test_result_log_has_exact_text_and_real_summary(self):
        from nibe_connectivity_check import run_connectivity_check
        p1, p2 = self._patch_both(True, True)
        with p1, p2, patch('nibe_connectivity_check.log_commands') as mock_log:
            result = run_connectivity_check('192.0.2.1', 'https://192.0.2.1:8443/api/v1/devices/0')
        mock_log.info.assert_any_call(
            "Connectivity check result: %s", result['summary'],
        )

    def test_both_succeed_overall_ok(self):
        from nibe_connectivity_check import run_connectivity_check
        p1, p2 = self._patch_both(True, True)
        with p1, p2:
            result = run_connectivity_check('192.0.2.1', 'https://192.0.2.1:8443/api/v1/devices/0')
        self.assertTrue(result['ok'])
        self.assertIn('Reachable', result['summary'])

    def test_both_fail_overall_not_ok(self):
        from nibe_connectivity_check import run_connectivity_check
        p1, p2 = self._patch_both(False, False)
        with p1, p2:
            result = run_connectivity_check('192.0.2.1', 'https://192.0.2.1:8443/api/v1/devices/0')
        self.assertFalse(result['ok'])
        self.assertIn('Unreachable', result['summary'])

    def test_ping_fails_curl_succeeds_not_ok_but_distinct_summary(self):
        """ICMP-blocked-but-HTTPS-fine is a real, common, benign case
        (firewalls often block ping specifically) — must not be reported
        the same way as a full outage."""
        from nibe_connectivity_check import run_connectivity_check
        p1, p2 = self._patch_both(False, True)
        with p1, p2:
            result = run_connectivity_check('192.0.2.1', 'https://192.0.2.1:8443/api/v1/devices/0')
        self.assertFalse(result['ok'])
        self.assertIn('ICMP may be blocked', result['summary'])

    def test_ping_and_curl_both_fail_but_curl_got_an_http_response_is_not_reported_as_full_outage(self):
        """Regression: a device that's up and answering HTTP (just with an
        unexpected/error status, e.g. 500) while ICMP is blocked must not be
        reported the same way as a genuine network/firewall outage — curl
        getting *any* http_code means the host was actually reached, even
        though curl_result['ok'] is False for that status."""
        from nibe_connectivity_check import run_connectivity_check
        with patch('nibe_connectivity_check._run_ping',
                   return_value={'ok': False, 'summary': 'ping timed out'}), \
             patch('nibe_connectivity_check._run_curl',
                   return_value={
                       'ok': False, 'http_code': 500,
                       'summary': 'Reachable, but got an unexpected HTTP 500.',
                   }):
            result = run_connectivity_check(
                '192.0.2.1', 'https://192.0.2.1:8443/api/v1/devices/0')
        self.assertFalse(result['ok'])
        self.assertNotIn('Unreachable', result['summary'])
        self.assertIn('HTTP 500', result['summary'])

    def test_ping_ok_curl_got_error_status_reports_curl_summary_not_generic_message(self):
        """Regression: when ping succeeds but curl reaches the host and gets
        back an error status (e.g. 500), the summary must reflect that the
        REST API actually responded — not the generic 'did not respond'
        port/firewall message, which was previously used for both this case
        and a genuine connection-level curl failure."""
        from nibe_connectivity_check import run_connectivity_check
        with patch('nibe_connectivity_check._run_ping',
                   return_value={'ok': True, 'summary': 'x'}), \
             patch('nibe_connectivity_check._run_curl',
                   return_value={
                       'ok': False, 'http_code': 500,
                       'summary': 'Reachable, but got an unexpected HTTP 500.',
                   }):
            result = run_connectivity_check(
                '192.0.2.1', 'https://192.0.2.1:8443/api/v1/devices/0')
        self.assertFalse(result['ok'])
        self.assertIn('HTTP 500', result['summary'])
        self.assertNotIn('port/service/firewall', result['summary'])

    def test_ping_succeeds_curl_fails_distinct_summary(self):
        """Host is up but the REST API specifically is unreachable — a
        different, more actionable diagnosis than a full network outage."""
        from nibe_connectivity_check import run_connectivity_check
        p1, p2 = self._patch_both(True, False)
        with p1, p2:
            result = run_connectivity_check('192.0.2.1', 'https://192.0.2.1:8443/api/v1/devices/0')
        self.assertFalse(result['ok'])
        self.assertIn('port/service/firewall', result['summary'])

    def test_result_includes_both_sub_results(self):
        from nibe_connectivity_check import run_connectivity_check
        p1, p2 = self._patch_both(True, True)
        with p1, p2:
            result = run_connectivity_check('192.0.2.1', 'https://192.0.2.1:8443/api/v1/devices/0')
        self.assertEqual(result['ping']['summary'], 'ping result')
        self.assertEqual(result['curl']['summary'], 'curl result')

    def test_ping_invoked_with_the_real_host(self):
        from nibe_connectivity_check import run_connectivity_check
        with patch('nibe_connectivity_check._run_ping',
                   return_value={'ok': True, 'summary': 'x'}) as mock_ping, \
             patch('nibe_connectivity_check._run_curl',
                   return_value={'ok': True, 'http_code': 200, 'summary': 'y'}):
            run_connectivity_check('192.0.2.9', 'https://192.0.2.9:8443/api/v1/devices/0')
        mock_ping.assert_called_once_with('192.0.2.9')

    def test_both_succeed_exact_summary(self):
        from nibe_connectivity_check import run_connectivity_check
        p1, p2 = self._patch_both(True, True)
        with p1, p2:
            result = run_connectivity_check('192.0.2.1', 'https://192.0.2.1:8443/api/v1/devices/0')
        self.assertEqual(result['summary'], 'Reachable — both ping and the REST API responded.')

    def test_both_fail_exact_summary(self):
        from nibe_connectivity_check import run_connectivity_check
        p1, p2 = self._patch_both(False, False)
        with p1, p2:
            result = run_connectivity_check('192.0.2.1', 'https://192.0.2.1:8443/api/v1/devices/0')
        self.assertEqual(
            result['summary'],
            'Unreachable — no response to ping or the REST API. Likely a network/firewall/VLAN block.',
        )

    def test_ping_fails_curl_succeeds_exact_summary(self):
        from nibe_connectivity_check import run_connectivity_check
        p1, p2 = self._patch_both(False, True)
        with p1, p2:
            result = run_connectivity_check('192.0.2.1', 'https://192.0.2.1:8443/api/v1/devices/0')
        self.assertEqual(
            result['summary'],
            'REST API responded but ping did not — ICMP may be blocked while HTTPS is allowed; '
            'not necessarily a problem.',
        )

    def test_ping_succeeds_curl_fails_no_response_exact_summary(self):
        from nibe_connectivity_check import run_connectivity_check
        p1, p2 = self._patch_both(True, False)
        with p1, p2:
            result = run_connectivity_check('192.0.2.1', 'https://192.0.2.1:8443/api/v1/devices/0')
        self.assertEqual(
            result['summary'],
            'Host responds to ping but the REST API did not — check the port/service/firewall '
            'for that specific port.',
        )

    def test_auth_rejected_401_with_ping_ok_uses_curl_summary(self):
        from nibe_connectivity_check import run_connectivity_check
        with patch('nibe_connectivity_check._run_ping',
                   return_value={'ok': True, 'summary': 'ping result'}), \
             patch('nibe_connectivity_check._run_curl',
                   return_value={'ok': False, 'http_code': 401, 'summary': 'credentials rejected'}):
            result = run_connectivity_check('192.0.2.1', 'https://192.0.2.1:8443/api/v1/devices/0')
        self.assertEqual(result['summary'], 'credentials rejected')

    def test_auth_rejected_403_with_ping_ok_uses_curl_summary(self):
        """Both 401 and 403 must be treated as credential rejection — a
        mutant narrowing the membership check to only one code would miss
        the other."""
        from nibe_connectivity_check import run_connectivity_check
        with patch('nibe_connectivity_check._run_ping',
                   return_value={'ok': True, 'summary': 'ping result'}), \
             patch('nibe_connectivity_check._run_curl',
                   return_value={'ok': False, 'http_code': 403, 'summary': 'credentials rejected 403'}):
            result = run_connectivity_check('192.0.2.1', 'https://192.0.2.1:8443/api/v1/devices/0')
        self.assertEqual(result['summary'], 'credentials rejected 403')

    def test_auth_rejected_with_ping_also_failed_appends_ping_note(self):
        from nibe_connectivity_check import run_connectivity_check
        with patch('nibe_connectivity_check._run_ping',
                   return_value={'ok': False, 'summary': 'ping result'}), \
             patch('nibe_connectivity_check._run_curl',
                   return_value={'ok': False, 'http_code': 401, 'summary': 'credentials rejected'}):
            result = run_connectivity_check('192.0.2.1', 'https://192.0.2.1:8443/api/v1/devices/0')
        self.assertEqual(result['summary'], 'credentials rejected Also, ping did not respond.')

    def test_http_code_402_is_not_treated_as_auth_rejected(self):
        """402 is adjacent to 401 but is not one of the two real
        credential-rejection codes (401/403) — it must fall through to the
        generic unexpected-status path, not the auth-rejected summary."""
        from nibe_connectivity_check import run_connectivity_check
        with patch('nibe_connectivity_check._run_ping',
                   return_value={'ok': True, 'summary': 'x'}), \
             patch('nibe_connectivity_check._run_curl',
                   return_value={'ok': False, 'http_code': 402, 'summary': 'unexpected HTTP 402'}):
            result = run_connectivity_check('192.0.2.1', 'https://192.0.2.1:8443/api/v1/devices/0')
        self.assertEqual(result['summary'], 'unexpected HTTP 402')

    def test_ca_cert_path_and_auth_header_forwarded_to_curl(self):
        from nibe_connectivity_check import run_connectivity_check
        with patch('nibe_connectivity_check._run_ping',
                   return_value={'ok': True, 'summary': 'x'}), \
             patch('nibe_connectivity_check._run_curl',
                   return_value={'ok': True, 'summary': 'x', 'http_code': 200}) as mock_curl:
            run_connectivity_check(
                '192.0.2.1', 'https://192.0.2.1:8443/api/v1/devices/0',
                ca_cert_path='/ssl/nibe-ca.pem', auth_header='Basic dGVzdA==',
            )
        mock_curl.assert_called_once_with(
            'https://192.0.2.1:8443/api/v1/devices/0', '/ssl/nibe-ca.pem', 'Basic dGVzdA==',
        )

    def test_credentials_rejected_reported_distinctly_from_network_failure(self):
        """A 401 from curl (credentials wrong) must produce a message
        naming the credentials problem — not the generic 'Unreachable...
        network/firewall/VLAN block' message, which would send a user
        chasing the wrong fix entirely."""
        from nibe_connectivity_check import run_connectivity_check
        with patch('nibe_connectivity_check._run_ping',
                   return_value={'ok': True, 'summary': 'x'}), \
             patch('nibe_connectivity_check._run_curl',
                   return_value={
                       'ok': False, 'http_code': 401,
                       'summary': 'Reachable, but credentials were rejected (HTTP 401) '
                                  '— check nibe_username/nibe_password in add-on options, '
                                  'or nibe_basic_auth in secrets.yaml.',
                   }):
            result = run_connectivity_check(
                '192.0.2.1', 'https://192.0.2.1:8443/api/v1/devices/0',
                auth_header='Basic d3Jvbmc=',
            )
        self.assertFalse(result['ok'])
        self.assertIn('credentials were rejected', result['summary'])
        self.assertNotIn('network/firewall/VLAN', result['summary'])

    def test_credentials_rejected_and_ping_failed_mentions_both(self):
        """Regression: when curl gets a 401/403 the summary used to always
        become curl's own summary verbatim, silently dropping a concurrent
        ping failure. ICMP-blocked-and-wrong-credentials is a real
        combination (a strict firewall drops ping while still allowing
        HTTPS through with an outdated auth header) and both problems must
        be visible, not just the credentials one."""
        from nibe_connectivity_check import run_connectivity_check
        with patch('nibe_connectivity_check._run_ping',
                   return_value={'ok': False, 'summary': 'ping timed out'}), \
             patch('nibe_connectivity_check._run_curl',
                   return_value={
                       'ok': False, 'http_code': 401,
                       'summary': 'Reachable, but credentials were rejected (HTTP 401) '
                                  '— check nibe_username/nibe_password in add-on options, '
                                  'or nibe_basic_auth in secrets.yaml.',
                   }):
            result = run_connectivity_check(
                '192.0.2.1', 'https://192.0.2.1:8443/api/v1/devices/0',
                auth_header='Basic d3Jvbmc=',
            )
        self.assertFalse(result['ok'])
        self.assertIn('credentials were rejected', result['summary'])
        self.assertIn('ping', result['summary'].lower())


if __name__ == '__main__':
    unittest.main()
