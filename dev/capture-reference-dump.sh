#!/bin/bash
# ==============================================================================
# dev/capture-reference-dump.sh — refresh reference-dumps/all_points_<lang>.json
# ==============================================================================
#
# Captures the controller's full point list, once per language, straight from
# the Nibe local REST API. These dumps are what the e2e harness replays and
# what every "is this point really classified right?" question gets checked
# against, so they need to match the firmware actually running.
#
# There was no script for this before: the dumps were captured by hand and the
# procedure lived in nobody's notes. They were last refreshed on 2026-09-04 and
# went stale at the very next firmware update (4.13.12), which added thirteen
# points — exactly the situation where an up-to-date dump matters most.
#
# The dumps are gitignored developer-local data (real serial numbers, real
# values from a real installation); this script never commits anything.
#
# Usage:
#   NIBE_PASSWORD=... ./dev/capture-reference-dump.sh --host 192.168.1.50 --user admin
#   ./dev/capture-reference-dump.sh --host nibe.local --user admin   # prompts for the password
#
# Options:
#   --host H       controller hostname or IP (or $NIBE_HOST)
#   --port P       HTTPS port, default 8443 (or $NIBE_PORT)
#   --user U       API username (or $NIBE_USERNAME)
#   --langs "..."  space-separated language codes, default "en nl de sv"
#   --out DIR      output directory, default reference-dumps/ at the repo root
#
# The password comes from $NIBE_PASSWORD or an interactive prompt — never a
# command-line flag, which would put it in your shell history and in `ps`.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

HOST="${NIBE_HOST:-}"
PORT="${NIBE_PORT:-8443}"
USERNAME="${NIBE_USERNAME:-}"
LANGS="en nl de sv"
OUT_DIR="reference-dumps"

while [ $# -gt 0 ]; do
    case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --user) USERNAME="$2"; shift 2 ;;
        --langs) LANGS="$2"; shift 2 ;;
        --out) OUT_DIR="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "Unknown argument: $1 (use -h for usage)" >&2
            exit 1
            ;;
    esac
done

if [ -z "$HOST" ]; then
    echo "No controller host given — pass --host or set NIBE_HOST." >&2
    exit 1
fi
if [ -z "$USERNAME" ]; then
    echo "No API username given — pass --user or set NIBE_USERNAME." >&2
    exit 1
fi

PASSWORD="${NIBE_PASSWORD:-}"
if [ -z "$PASSWORD" ]; then
    # -s so it is not echoed; prompt goes to stderr so stdout stays clean.
    printf 'API password for %s@%s: ' "$USERNAME" "$HOST" >&2
    read -r -s PASSWORD
    printf '\n' >&2
fi

mkdir -p "$OUT_DIR"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

BASE="https://${HOST}:${PORT}/api/v1/devices/0/points"
echo "==> Capturing from $BASE"

for lang in $LANGS; do
    target="${OUT_DIR}/all_points_${lang}.json"
    tmp="${TMP_DIR}/${lang}.json"

    # -k because the controller serves a self-signed certificate with no CA
    # chain available — the same reason app/nibe_api.py disables verification.
    # --fail turns an HTTP error into a non-zero exit instead of a body that
    # would otherwise be written over a perfectly good dump.
    if ! curl -sS -k --fail --max-time 60 \
        -u "${USERNAME}:${PASSWORD}" \
        -H "Accept-Language: ${lang}" \
        -o "$tmp" \
        "$BASE"; then
        echo "  ${lang}: request failed — leaving the existing dump untouched" >&2
        continue
    fi

    # Validate before replacing anything. These files are read back with their
    # parse failure swallowed as "no data" in several places, so a truncated or
    # error-page dump is worse than a stale one: it fails silently and takes
    # the good copy with it.
    count=$(python3 -c '
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception as exc:
    print(f"not valid JSON: {exc}", file=sys.stderr)
    sys.exit(1)
if not isinstance(data, dict) or not data:
    print("expected a non-empty object keyed by point id", file=sys.stderr)
    sys.exit(1)
print(len(data))
' "$tmp") || {
        echo "  ${lang}: response did not look like a point dump — keeping the existing file" >&2
        continue
    }

    if [ -f "$target" ]; then
        python3 -c '
import json, sys

old = json.load(open(sys.argv[1]))
new = json.load(open(sys.argv[2]))
added = sorted(int(k) for k in new.keys() - old.keys())
removed = sorted(int(k) for k in old.keys() - new.keys())
lang = sys.argv[3]
print(f"  {lang}: {len(new)} points ({len(old)} before)")
if added:
    print(f"    added:   {added}")
if removed:
    print(f"    removed: {removed}")
if not added and not removed:
    print("    no point ids added or removed")
' "$target" "$tmp" "$lang"
    else
        echo "  ${lang}: ${count} points (no previous dump to compare against)"
    fi

    # Replace atomically, so an interrupted copy cannot leave a half-written
    # dump behind — same reasoning as _atomic_write_text in the add-on.
    mv "$tmp" "$target"
done

echo "==> Done. Dumps are gitignored developer-local data; nothing was committed."
