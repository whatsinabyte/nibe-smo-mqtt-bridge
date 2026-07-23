"""
nibe_api.py
===========
NibeApiClient — all HTTP communication with the Nibe SMO S40 REST API.

Responsibilities
----------------
- Executing GET / PATCH / POST / DELETE requests against the Nibe API.
- Handling authentication, TLS (self-signed cert), retries, and error mapping.
- Fetching the bulk point data, individual points, device info, notifications,
  and device-mode endpoints (aid mode, smart mode).

Nothing in this module knows about MQTT, Home Assistant, or entity types.
All callers receive plain Python dicts / booleans and handle MQTT/HA concerns
themselves.

Public surface
--------------
NibeApiClient(base_url, auth, ssl_context)
    .request(url, method, data)       → dict | None   (raises on auth/404)
    .fetch_bulk_points()              → dict | None
    .fetch_device_info()              → dict | None
    .fetch_notifications()            → list | None
    .reset_notifications()            → bool
    .write_point(point_id, value)     → bool
    .write_device_mode(mode, value)   → bool
"""

import json
import random
import ssl
import time
import urllib.error
import urllib.request
import logging


log_api      = logging.getLogger("nibe.api")
log_commands = logging.getLogger("nibe.commands")

# ── Retry / backoff constants ──────────────────────────────────────────────────
# The API client retries once on transient errors.  The delay uses full jitter
# (random in [0, base]) to avoid thundering-herd if multiple components retry
# simultaneously after a network event.
_RETRY_BASE_S  = 2.0   # base delay in seconds
_RETRY_MAX_S   = 10.0  # cap (relevant if base is increased in future)

def _retry_delay() -> float:
    """Return a jittered backoff delay in seconds.

    Uses full jitter: ``random.uniform(0, min(base, cap))``.  This prevents
    correlated retries when multiple callers hit the same transient failure.
    """
    return random.uniform(0, min(_RETRY_BASE_S, _RETRY_MAX_S))


class NibeApiClient:
    """HTTP client for the Nibe SMO S40 local REST API.

    Parameters
    ----------
    base_url : str
        Root of the device API, e.g. ``https://192.168.2.201:8443/api/v1/devices/0``.
    auth : str
        HTTP Authorization header value, e.g. ``"Basic <token>"``.
    ssl_context : ssl.SSLContext
        Pre-built context with hostname verification disabled (self-signed cert).
    """

    def __init__(self, base_url: str, auth: str, ssl_context: ssl.SSLContext) -> None:
        self.base_url    = base_url
        self.auth        = auth
        self.ssl_context = ssl_context

    # ------------------------------------------------------------------ #
    # Low-level request                                                    #
    # ------------------------------------------------------------------ #

    def request(
        self,
        url: str,
        method: str = 'GET',
        data: str | None = None,
    ) -> dict | None:
        """Send an HTTP request and return the parsed JSON body, or None.

        Returns None on recoverable errors (non-auth HTTP errors, network issues)
        so callers treat a failed fetch as a temporary outage rather than a crash.
        Raises urllib.error.HTTPError for auth (401/403) and 404 errors because
        those signal configuration problems or genuinely missing resources that
        callers must handle specifically.

        A single automatic retry is attempted after a jittered delay for
        transient network errors.  Auth errors and 404s are never retried.
        """
        headers = {
            'Authorization': self.auth,  # pragma: no mutate
            'Accept':        'application/json',  # pragma: no mutate
        }
        if data:
            headers['Content-Type'] = 'application/json'

        body = data.encode() if isinstance(data, str) else data
        req  = urllib.request.Request(url, data=body, headers=headers, method=method)

        for attempt in range(2):   # attempt 0 = first try, attempt 1 = single retry
            last_attempt = (attempt == 1)
            try:
                response = urllib.request.urlopen(req, context=self.ssl_context, timeout=30)  # pragma: no mutate
                return json.loads(response.read().decode())

            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    log_api.error(
                        "API authentication failed (HTTP %d) for %s — check credentials",
                        e.code, url,
                    )  # pragma: no mutate
                    raise
                if e.code == 404:
                    raise
                log_api.warning(
                    "HTTP %d from %s — %s",
                    e.code, url, "giving up" if last_attempt else "retrying with backoff",
                )  # pragma: no mutate
                if last_attempt:
                    return None

            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
                log_api.warning(
                    "Request to %s failed: %s — %s",
                    url, e, "giving up" if last_attempt else "retrying with backoff",
                )  # pragma: no mutate
                if last_attempt:
                    return None

            except Exception as e:
                log_api.error(
                    "Unexpected error in request to %s: %s — this is likely a bug",
                    url, e, exc_info=True,
                )  # pragma: no mutate
                return None

            # Transient failure on first attempt — sleep before retry
            delay = _retry_delay()
            log_api.debug("Retry delay: %.2fs", delay)  # pragma: no mutate
            time.sleep(delay)

        return None  # unreachable; satisfies type checkers


    # ------------------------------------------------------------------ #
    # High-level fetch methods                                             #
    # ------------------------------------------------------------------ #

    def fetch_device_info(self) -> dict | None:
        """GET the root device endpoint for product / serial / firmware info."""
        return self.request(self.base_url)

    def fetch_bulk_points(self) -> dict | None:
        """GET /points — return the full dict of all data points, or None."""
        return self.request(f"{self.base_url}/points")

    def fetch_point(self, point_id: int) -> dict | None:
        """GET /points/{point_id} — return a single point dict, or None.

        Returns None on network errors and on HTTP 404.  The Nibe API returns
        404 when a dynamic point's controlling condition is currently inactive
        (firmware deviation #3 — undocumented; spec only documents 200/401/403).
        Callers treat None uniformly as "point unavailable" regardless of cause.
        """
        try:
            result = self.request(f"{self.base_url}/points/{point_id}")
            log_api.debug(
                "fetch_point(%d) → %s",
                point_id,
                repr(result)[:120] if result is not None else "None",
            )  # pragma: no mutate
            return result
        except urllib.error.HTTPError as e:
            log_api.debug("fetch_point(%d) → HTTP %d", point_id, e.code)  # pragma: no mutate
            if e.code == 404:
                log_api.debug(
                    "fetch_point(%d): point absent (dynamic point inactive "
                    "or does not exist at this firmware version)", point_id,
                )  # pragma: no mutate
                return None
            raise

    def fetch_notifications(self) -> list[dict] | None:
        """GET /notifications — return the alarm list, or None on error."""
        response = self.request(f"{self.base_url}/notifications")
        if response is None:
            return None
        return response.get('alarms', [])

    # ------------------------------------------------------------------ #
    # Write methods                                                        #
    # ------------------------------------------------------------------ #

    def write_point(self, point_id: int, value: int, entity_info: dict) -> bool:
        """PATCH /points to write a value to a register.

        Parameters
        ----------
        point_id :
            Nibe variableId of the register to write.
        value :
            Raw integer value (pre-divisor) to write.
        entity_info :
            The entity_info dict from EntityManager.  Used for writability,
            range checks, and degenerate-range detection.

        Returns True on success, False on any failure.
        """
        metadata = entity_info.get('metadata', {})

        if not entity_info.get('is_writable', False):
            log_commands.warning("Point %d is not writable", point_id)  # pragma: no mutate
            return False

        min_val      = metadata.get('minValue')
        max_val      = metadata.get('maxValue')
        is_degenerate = entity_info.get('is_degenerate_range', False)

        if not is_degenerate:
            if min_val is not None and value < min_val:
                log_commands.warning(
                    "Value %s below minimum %s for point %d", value, min_val, point_id
                )  # pragma: no mutate
                return False
            if max_val is not None and value > max_val:
                log_commands.warning(
                    "Value %s above maximum %s for point %d", value, max_val, point_id
                )  # pragma: no mutate
                return False

        payload = json.dumps([{
            "type":         "datavalue",   # pragma: no mutate
            "variableId":   point_id,      # pragma: no mutate
            "integerValue": value,         # pragma: no mutate
            "stringValue":  None,          # pragma: no mutate
        }])

        url = f"{self.base_url}/points"
        try:
            req      = urllib.request.Request(
                url,
                data=payload.encode(),
                headers={
                    'Authorization': self.auth,  # pragma: no mutate
                    'Accept':        'application/json',  # pragma: no mutate
                    'Content-Type':  'application/json',  # pragma: no mutate
                },
                method='PATCH',
            )
            response     = urllib.request.urlopen(req, context=self.ssl_context, timeout=30)  # pragma: no mutate
            data_json    = json.loads(response.read().decode())
            point_resp   = data_json.get(str(point_id))

            # Accept both the documented string response and the actual full-object
            # response returned by SMO S40 firmware.
            if point_resp == "modified":
                return True
            if isinstance(point_resp, dict):
                dv = point_resp.get('value', {})
                if dv.get('isOk'):
                    log_commands.debug(
                        "Write confirmed for point %d (firmware full-object response)", point_id
                    )  # pragma: no mutate
                    return True
                log_commands.error(
                    "Write for point %d: firmware returned object but isOk=False "
                    "(value may not have been committed)", point_id
                )  # pragma: no mutate
                return False

            if point_resp == "error: no such param":
                log_commands.error(
                    "Write rejected for point %d: register does not exist in this firmware version",
                    point_id,
                )  # pragma: no mutate
            elif point_resp == "error: read only value":
                log_commands.error(
                    "Write rejected for point %d: register is read-only "
                    "(check entity configuration)", point_id,
                )  # pragma: no mutate
            else:
                log_commands.error(
                    "Write for point %d: unexpected API response: %r "
                    "(expected 'modified' or point object)",
                    point_id, point_resp,
                )  # pragma: no mutate
            return False

        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode('utf-8', errors='replace')  # pragma: no mutate
            except Exception as body_err:
                log_commands.debug(
                    "Could not read HTTP %d error body for point %d: %s",
                    e.code, point_id, body_err,
                )  # pragma: no mutate
            if e.code == 400:  # pragma: no mutate
                log_commands.error("Write rejected for point %d (HTTP 400): %s", point_id, body)  # pragma: no mutate
            elif e.code == 401:  # pragma: no mutate
                log_commands.error("Write rejected for point %d: auth invalid (HTTP 401)", point_id)  # pragma: no mutate
            elif e.code == 403:  # pragma: no mutate
                log_commands.error("Write rejected for point %d: wrong deviceId (HTTP 403)", point_id)  # pragma: no mutate
            else:
                log_commands.error("Write HTTP %d for point %d: %s", e.code, point_id, body)  # pragma: no mutate
            return False
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            log_commands.error("Network error writing point %d: %s", point_id, e)  # pragma: no mutate
            return False
        except Exception as e:
            log_commands.error("Unexpected error writing point %d: %s", point_id, e, exc_info=True)  # pragma: no mutate
            return False

    def reset_notifications(self) -> bool:
        """DELETE /notifications — clear all active alarms.

        Returns True on HTTP 204, False on any error.
        """
        headers = {
            'Authorization': self.auth,  # pragma: no mutate
            'Accept':        'application/json',  # pragma: no mutate
        }
        try:
            req = urllib.request.Request(
                f"{self.base_url}/notifications", headers=headers, method='DELETE'
            )
            urllib.request.urlopen(req, context=self.ssl_context, timeout=30)  # pragma: no mutate
            log_commands.info("Notifications reset: all alarms cleared")  # pragma: no mutate
            return True
        except urllib.error.HTTPError as e:
            if e.code == 405:  # pragma: no mutate
                log_commands.warning("Notifications reset not supported (HTTP 405)")  # pragma: no mutate
            elif e.code == 401:  # pragma: no mutate
                log_commands.error("Notifications reset: auth invalid (HTTP 401)")  # pragma: no mutate
            elif e.code == 403:  # pragma: no mutate
                log_commands.error("Notifications reset: wrong deviceId (HTTP 403)")  # pragma: no mutate
            else:
                log_commands.error("Notifications reset failed: HTTP %d", e.code)  # pragma: no mutate
            return False
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            log_commands.error("Network error resetting notifications: %s", e)  # pragma: no mutate
            return False
        except Exception as e:
            log_commands.error("Unexpected error resetting notifications: %s", e, exc_info=True)  # pragma: no mutate
            return False

    def write_device_mode(self, mode_type: str, value: str) -> bool:
        """POST /{mode_type} — write aid mode or smart mode.

        Parameters
        ----------
        mode_type : str
            One of "aidmode" or "smartmode".
        value : str
            The string value to write (e.g. "on"/"off" for aidmode,
            "normal"/"away" for smartmode).
        """
        url     = f"{self.base_url}/{mode_type}"
        payload = json.dumps({mode_type: value})
        try:
            req      = urllib.request.Request(
                url,
                data=payload.encode(),
                headers={
                    'Authorization': self.auth,  # pragma: no mutate
                    'Accept':        'application/json',  # pragma: no mutate
                    'Content-Type':  'application/json',  # pragma: no mutate
                },
                method='POST',
            )
            urllib.request.urlopen(req, context=self.ssl_context, timeout=30)  # pragma: no mutate
            log_commands.info("Device mode %s set to %s", mode_type, value)  # pragma: no mutate
            return True
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode('utf-8', errors='replace')  # pragma: no mutate
            except Exception as body_err:
                log_commands.debug(
                    "Could not read HTTP %d error body for device mode %s: %s",
                    e.code, mode_type, body_err,
                )  # pragma: no mutate
            if e.code == 400:  # pragma: no mutate
                log_commands.error("Device mode %s rejected (HTTP 400): %s", mode_type, body)  # pragma: no mutate
            elif e.code == 401:  # pragma: no mutate
                log_commands.error("Device mode %s: auth invalid (HTTP 401)", mode_type)  # pragma: no mutate
            elif e.code == 403:  # pragma: no mutate
                log_commands.error("Device mode %s: wrong deviceId (HTTP 403)", mode_type)  # pragma: no mutate
            else:
                log_commands.error("Device mode %s failed: HTTP %d — %s", mode_type, e.code, body)  # pragma: no mutate
            return False
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            log_commands.error("Network error setting device mode %s: %s", mode_type, e)  # pragma: no mutate
            return False
        except Exception as e:
            log_commands.error("Unexpected error setting device mode %s: %s", mode_type, e, exc_info=True)  # pragma: no mutate
            return False
