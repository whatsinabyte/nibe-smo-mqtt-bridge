"""Tests for nibe_connectivity_check.py — the independent ping+curl
diagnostic behind the "Test API Connection" debug management button."""

import subprocess
import unittest
from unittest.mock import MagicMock, patch


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


class TestRunConnectivityCheck(unittest.TestCase):
    """run_connectivity_check() combines ping + curl into one overall result."""

    def _patch_both(self, ping_ok, curl_ok):
        return (
            patch('nibe_connectivity_check._run_ping',
                  return_value={'ok': ping_ok, 'summary': 'ping result'}),
            patch('nibe_connectivity_check._run_curl',
                  return_value={'ok': curl_ok, 'summary': 'curl result', 'http_code': 200 if curl_ok else None}),
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


if __name__ == '__main__':
    unittest.main()
