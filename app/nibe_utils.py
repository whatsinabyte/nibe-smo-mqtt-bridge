"""
nibe_utils.py
=============
Small shared utilities used across multiple bridge modules.

Keeping these here avoids copy-pasting identical helpers into every module
and gives a single place to fix or extend them.

Nothing in this module holds state or imports from the rest of the bridge,
and nothing performs I/O except atomic_write_text — the one file-writing
helper, shared by every module that persists to /data.
"""

import contextlib
import os
import tempfile
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


def atomic_write_text(path: str, payload: str) -> None:
    """Write ``payload`` to ``path`` so a reader never sees a partial file.

    ``open(path, "w")`` truncates immediately, so a process killed partway
    through leaves an empty or half-written file behind. Every one of these
    fallback files is read back with a ``json.loads`` (or equivalent) whose
    failure is swallowed as "no data", so a truncated write doesn't surface
    as an error — it silently discards whatever it held. That matters here
    because being killed mid-write is a real scenario, not a theoretical
    one: the shutdown drain budget can exceed a supervisor's stop grace
    period, so SIGKILL during shutdown genuinely happens.

    Writing to a sibling temp file and renaming avoids it — ``os.replace``
    is atomic on POSIX, so the destination only ever contains the previous
    complete contents or the new complete contents.

    The temp file gets a unique name rather than a fixed ``path + ".tmp"``.
    None of the callers hold a common lock, and they are reachable from the
    MQTT command thread, the management executor, the registry watcher
    thread and the poll thread at once — two of them sharing one temp path
    would have each truncated the other's half-written file, and whichever
    renamed last would promote the resulting mixture to the destination.
    That is precisely the corruption this function exists to prevent. With
    distinct temp files each writer renames a complete payload and the last
    one wins, which is the expected outcome for concurrent writers.

    Raises ``OSError`` like a plain write would, so callers keep their own
    error handling.
    """
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{os.path.basename(path)}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        # mkstemp creates the file 0600; the plain open() this replaced left
        # it at the process umask (0644 in practice). Keep that, so nothing
        # reading these files sees a permissions change.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except OSError:
        # Don't leave the temp file behind on a failed write — /data is
        # small on the embedded targets this runs on.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
