"""
nibe_utils.py
=============
Small shared utilities used across multiple bridge modules.

Keeping these here avoids copy-pasting identical helpers into every module
and gives a single place to fix or extend them.

Nothing in this module performs I/O, holds state, or imports from the rest
of the bridge — the same constraint as nibe_entity_detection.py.
"""

import time

# Cipher-string suffix used to widen OpenSSL's TLS compatibility (below its
# default SECLEVEL=2) when talking to the Nibe controller's old embedded TLS
# stack. Shared by app/generate_nibe_mqtt.py::_build_ssl_context (the real
# polling connection) and app/nibe_connectivity_check.py::_run_curl (the
# "Test API Connection" diagnostic), so the diagnostic's TLS compatibility
# can't silently drift from what the real connection actually accepts.
TLS_COMPAT_CIPHERS = "DEFAULT@SECLEVEL=1"


def fmt_ts(t: float | None = None) -> str:
    """Return a ``YYYY-MM-DD HH:MM:SS`` timestamp string in local time.

    Parameters
    ----------
    t:
        Unix timestamp to format.  Defaults to the current time when omitted
        or ``None``.
    """
    return time.strftime(
        "%Y-%m-%d %H:%M:%S",
        time.localtime(t) if t is not None else time.localtime(),
    )
