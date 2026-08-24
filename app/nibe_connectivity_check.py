"""
nibe_connectivity_check.py
============================
Self-contained network diagnostic for the Nibe REST API, run on demand via
the "Test API Connection" debug management button.

Runs `ping` and `curl` as subprocesses rather than reusing NibeApiClient /
urllib deliberately: the whole point is an INDEPENDENT check that shares no
code, no bug, and no misconfiguration with the bridge's own HTTP client, so
a user (or a maintainer helping them) can tell "is this a bug in our
client" apart from "is this a genuine network/TLS/auth problem" from a
single result — this mirrors exactly the manual `ping` + `curl` sequence
users are asked to run from the HA host when diagnosing connectivity
issues, just built into the add-on so no SSH/terminal access is needed.

Responsibilities
-----------------
- run_connectivity_check(host, base_url, ca_cert_path, auth_header) — ping
  the host, then curl the REST API using the bridge's real configured TLS
  verification mode and credentials, and return a combined structured
  result that distinguishes a network problem from a TLS/CA problem from a
  credentials problem.

What this module does NOT do
------------------------------
- No MQTT publishing, no HA notifications (the caller, ManagementCommandHandler,
  handles reporting the result — same separation as nibe_test_runner.py).
"""

import logging
import subprocess

from nibe_utils import TLS_COMPAT_CIPHERS

log_commands = logging.getLogger('nibe.commands')

# curl exit codes that are worth a specific, actionable message rather than
# a bare "curl exited with code N". See `man curl` EXIT CODES.
_CURL_EXIT_MESSAGES: dict[int, str] = {
    6:  "Could not resolve host — check the configured host/IP.",
    7:  "Could not connect — device unreachable at this address/port "
        "(network/firewall/VLAN block, or the device is offline).",
    28: "Connection timed out — no response within the time limit "
        "(network unreachable, firewalled, or the device is overloaded).",
    35: "TLS handshake failed.",
    52: "Empty reply from server — connected, but the device closed the "
        "connection without responding.",
    56: "Connection reset while receiving data.",
}


def _run_ping(host: str, count: int = 3, timeout: int = 5) -> dict:
    """Ping *host* and return {'ok': bool, 'summary': str}.

    A bare L3 reachability check, independent of the REST API/TLS/auth
    entirely — distinguishes "nothing at this address responds at all"
    from "the device is up but the REST API specifically is unreachable".
    """
    try:
        result = subprocess.run(
            ['ping', '-c', str(count), '-W', str(timeout), host],
            capture_output=True, text=True, timeout=timeout * count + 5, check=False,
        )
    except FileNotFoundError:
        return {'ok': False, 'summary': "ping is not installed in this container."}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'summary': "ping did not exit within the expected time."}

    if result.returncode == 0:
        return {'ok': True, 'summary': f"{host} responds to ping."}
    if result.returncode == 1:
        return {'ok': False, 'summary': f"{host} does not respond to ping (no reply received)."}
    detail = result.stderr.strip() or result.stdout.strip()
    return {
        'ok': False,
        'summary': f"Could not ping {host}" + (f" — {detail}" if detail else "."),
    }


def _run_curl(
    base_url: str,
    ca_cert_path: str | None,
    auth_header: str | None = None,
    timeout: int = 10,
) -> dict:
    """Probe *base_url* with curl and return
    {'ok': bool, 'summary': str, 'http_code': int | None, 'tls_verified': bool}.

    When *auth_header* is given (the bridge's real, configured
    ``Authorization`` value), this exercises the exact same request the
    bridge itself makes — TLS verification mode included — so it can tell
    apart three distinct outcomes instead of just "reachable or not":
    a genuine 2xx success, credentials rejected (401/403 — reachable, TLS
    fine, but nibe_username/nibe_password/nibe_basic_auth is wrong), or a
    connection-level failure (network/TLS never even completed). Without
    *auth_header*, 'ok' is True for any real HTTP response (even 401/403) —
    a pure reachability check with no opinion on credentials.
    """
    url = f"{base_url}/points"
    cmd = ['curl', '-sS', '--max-time', str(timeout), '-o', '/dev/null',
           '-w', 'HTTP_CODE:%{http_code}']
    if ca_cert_path:
        cmd += ['--cacert', ca_cert_path]
    else:
        # Mirror _build_ssl_context's no-CA branch: widen TLS-version/cipher
        # compatibility the same way, so this diagnostic can't report
        # "unreachable" against a controller whose old TLS stack the real
        # polling connection (via NibeApiClient) already tolerates.
        cmd += ['-k', '--tlsv1.0', '--ciphers', TLS_COMPAT_CIPHERS]
    if auth_header:
        cmd += ['-H', f'Authorization: {auth_header}']
    cmd.append(url)

    tls_verified = bool(ca_cert_path)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout + 5, check=False,
        )
    except FileNotFoundError:
        return {'ok': False, 'summary': "curl is not installed in this container.",
                'http_code': None, 'tls_verified': tls_verified}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'summary': "curl did not exit within the expected time — treating as a hang.",
                'http_code': None, 'tls_verified': tls_verified}

    verified_note = (
        " (TLS verified against configured CA)" if ca_cert_path
        else " (TLS verification skipped — self-signed cert)"
    )

    if result.returncode != 0:
        reason = _CURL_EXIT_MESSAGES.get(
            result.returncode, f"curl exited with code {result.returncode}.",
        )
        detail = result.stderr.strip()
        summary = reason + (f" ({detail})" if detail and detail not in reason else "")
        return {'ok': False, 'summary': summary, 'http_code': None, 'tls_verified': tls_verified}

    http_code = None
    for line in result.stdout.splitlines():
        if line.startswith('HTTP_CODE:'):
            try:
                # maxsplit=1 vs omitted, split() vs rsplit(), and
                # maxsplit=2 are all equivalent for this exact string shape
                # (exactly one ':' in "HTTP_CODE:NNN") — not pragma'd, the
                # ':' separator itself, split(None,...) whitespace-splitting,
                # and index [1] vs [2] are all real/tested (crash/wrong
                # value on a real curl response).
                http_code = int(line.split(':', 1)[1])
            except ValueError:
                pass

    if auth_header is None:
        # Pure reachability mode — any real HTTP response counts as reachable.
        return {
            'ok': True,
            'summary': f"Reachable — HTTP {http_code} from the device{verified_note}.",
            'http_code': http_code,
            'tls_verified': tls_verified,
        }

    if http_code is not None and 200 <= http_code < 300:
        return {
            'ok': True,
            'summary': f"Reachable and authenticated — HTTP {http_code}{verified_note}.",
            'http_code': http_code,
            'tls_verified': tls_verified,
        }
    if http_code in (401, 403):
        return {
            'ok': False,
            'summary': (
                f"Reachable{verified_note}, but credentials were rejected (HTTP {http_code}) "
                "— check nibe_username/nibe_password in add-on options, or nibe_basic_auth in secrets.yaml."
            ),
            'http_code': http_code,
            'tls_verified': tls_verified,
        }
    return {
        'ok': False,
        'summary': f"Reachable{verified_note}, but got an unexpected HTTP {http_code}.",
        'http_code': http_code,
        'tls_verified': tls_verified,
    }


def run_connectivity_check(
    host: str,
    base_url: str,
    ca_cert_path: str | None = None,
    auth_header: str | None = None,
) -> dict:
    """Run the combined ping + curl diagnostic and return a structured result.

    Exercises the same host/port, TLS/CA verification mode, and credentials
    the bridge's own NibeApiClient is actually configured with — so a
    single run of this check can distinguish a network problem, a TLS/CA
    problem, and a credentials problem from each other, matching the full
    set of config options (nibe_host, nibe_port, nibe_ca_cert,
    nibe_username/nibe_password/nibe_basic_auth) that affect whether the
    bridge can reach the controller at all.

    Returns
    -------
    dict with keys:
      'ok'      : bool — True only if both ping and curl succeeded
      'ping'    : the _run_ping() result dict
      'curl'    : the _run_curl() result dict
      'summary' : one-line overall summary for the HA notification title/state
    """
    # host/base_url value substitutions and message text are log-only (both
    # reused for real right after, unaffected by this log call's own copy)
    # — not pragma'd, arg count/format string are real/tested.
    log_commands.info("Running connectivity check against %s (%s)", host, base_url)
    ping_result = _run_ping(host)
    curl_result = _run_curl(base_url, ca_cert_path, auth_header)

    ok = ping_result['ok'] and curl_result['ok']
    # Wrong key, always-None, or a wrong/narrowed code tuple here all leave
    # auth_rejected falsy in every case that would otherwise have been
    # True — but whenever the real value WOULD be True, curl also reached
    # the host (http_code is not None), so the summary always falls through
    # to the `elif curl_reached_host:` / `elif not ping_result['ok']:`
    # branches below with the identical `curl_result['summary']` /
    # `also_ping_failed` text anyway. Verified empirically: none of these
    # mutations change any assertion in test_connectivity_check.py. Not
    # pragma'd because `in` -> `not in` on this same line IS real (it can
    # make auth_rejected wrongly True when curl succeeded with a non-401/403
    # code) and is already caught by
    # test_ping_fails_curl_succeeds_exact_summary.
    auth_rejected = curl_result.get('http_code') in (401, 403)
    # http_code is set whenever curl got any real HTTP response, even one
    # that makes curl_result['ok'] False (e.g. a 500) — distinct from curl
    # never completing a connection at all (http_code is None). Conflating
    # the two would misreport a device that's up but erroring server-side
    # as a full network/firewall outage.
    curl_reached_host = curl_result.get('http_code') is not None
    also_ping_failed  = f"{curl_result['summary']} Also, ping did not respond."
    if ok:
        summary = "Reachable — both ping and the REST API responded."
    elif not ping_result['ok'] and not curl_reached_host:
        summary = "Unreachable — no response to ping or the REST API. Likely a network/firewall/VLAN block."
    elif auth_rejected and ping_result['ok']:
        summary = curl_result['summary']
    elif auth_rejected:
        summary = also_ping_failed
    elif not ping_result['ok'] and curl_result['ok']:
        summary = "REST API responded but ping did not — ICMP may be blocked while HTTPS is allowed; not necessarily a problem."
    elif not ping_result['ok']:
        summary = also_ping_failed
    elif curl_reached_host:
        summary = curl_result['summary']
    else:
        summary = "Host responds to ping but the REST API did not — check the port/service/firewall for that specific port."

    # summary value substitution and message text are log-only — not
    # pragma'd, arg count/format string are real/tested.
    log_commands.info("Connectivity check result: %s", summary)
    return {'ok': ok, 'ping': ping_result, 'curl': curl_result, 'summary': summary}
