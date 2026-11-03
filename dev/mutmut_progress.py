"""Turn mutmut's redrawn status line into a readable progress log.

mutmut redraws its progress line with carriage returns, which ends up as
one endless line in a log file or a background job's output. This reads
mutmut's output and prints a status line at most every ``interval``
seconds (default 30), prefixed with the time elapsed, plus every other
line as it comes, so a long run shows it is still moving.

Usage: mutmut run ... 2>&1 | python dev/mutmut_progress.py [interval]
"""

import re
import sys
import time

# A status line: a spinner or count followed by "done/total".
_STATUS = re.compile(r"\d+/\d+")


def main():
    interval = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    started = time.monotonic()
    last_shown = 0.0
    pending = ""
    buffer = ""
    while True:
        chunk = sys.stdin.read(1)
        if not chunk:
            break
        if chunk not in "\r\n":
            buffer += chunk
            continue
        line, buffer = buffer.strip(), ""
        if not line:
            continue
        now = time.monotonic()
        elapsed = time.strftime("%H:%M:%S", time.gmtime(now - started))
        if _STATUS.search(line):
            pending = line
            if now - last_shown >= interval:
                print(f"[{elapsed}] {line}", flush=True)
                last_shown, pending = now, ""
        else:
            print(f"[{elapsed}] {line}", flush=True)
    if pending:
        elapsed = time.strftime("%H:%M:%S", time.gmtime(time.monotonic() - started))
        print(f"[{elapsed}] {pending}", flush=True)


if __name__ == "__main__":
    main()
