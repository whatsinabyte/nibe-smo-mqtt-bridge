#!/bin/bash
# ==============================================================================
# dev/e2e/run.sh — one-command runner for the real-stack e2e harness.
# ==============================================================================
#
# Wraps the manual sequence documented in dev/e2e/README.md ("How to run it")
# into a single script: bring up mosquitto/mock-api/HA, wait for HA to answer,
# headlessly seed it (onboarding + MQTT integration), start the bridge, wait
# for it to report ready, restart HA once so it picks up the card JS the
# bridge just copied into the shared /config/www volume, then run the one
# Playwright test.
#
# This does not replace understanding what each step does — read
# dev/e2e/README.md first, especially "How HA is brought to a usable,
# unattended state" and "Known gap: Lovelace auto-provisioning requires a
# real Supervisor". This script is a convenience wrapper around exactly the
# steps documented there, nothing more.
#
# Usage:
#   ./run.sh              Fresh run: builds/starts everything, seeds HA, runs the test.
#   ./run.sh --keep-open   Same, but skips teardown at the end (stack stays up for poking around).
#   ./run.sh --down        Tear down only (equivalent to the README's "Tear down" section).
#
# Idempotency note (see README): re-running this script against an
# already-seeded, still-running stack is fine — onboarding steps that are
# already done are skipped. Re-running it against a *stopped and restarted*
# stack reusing the same ha-config volume is NOT supported (HA's onboarding
# can only run once per volume) — this script always starts from `docker
# compose down -v` for that reason, so every run is a genuinely clean one.
# Use --keep-open if you want to inspect or iterate against a live stack
# without tearing it down between runs.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

KEEP_OPEN=0
DOWN_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --keep-open) KEEP_OPEN=1 ;;
        --down) DOWN_ONLY=1 ;;
        -h|--help)
            sed -n '2,31p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg (use -h for usage)" >&2
            exit 1
            ;;
    esac
done

teardown() {
    echo "==> Tearing down (docker compose down -v)"
    docker compose down -v
    rm -f seed-out/*.json seed-out/*.txt
}

if [ "$DOWN_ONLY" -eq 1 ]; then
    teardown
    exit 0
fi

if [ ! -d node_modules ]; then
    echo "==> Installing JS deps (first run only)"
    npm install
    npx playwright install --with-deps chromium
fi

if [ ! -f ../../reference-dumps/all_points_en.json ]; then
    echo "reference-dumps/all_points_en.json not found at the repo root." >&2
    echo "This is gitignored, developer-local reference data — see CONTRIBUTING.md." >&2
    exit 1
fi

# Always start from a clean slate — reusing a stopped/restarted ha-config
# volume is not supported (see the idempotency note above and README.md).
echo "==> Starting from a clean slate"
docker compose down -v >/dev/null 2>&1 || true
rm -f seed-out/*.json seed-out/*.txt

echo "==> Bringing up mosquitto, mock-nibe-api, homeassistant"
# --build for the same reason it is not optional on the bridge below: the
# mock API's script is COPYed into its image, so without this an edit to
# mock-api/mock_nibe_api.py is silently ignored and every run keeps testing
# against whatever image was built first. mosquitto and homeassistant are
# pulled images with no build context, so the flag is a no-op for them.
docker compose up -d --build mosquitto mock-nibe-api homeassistant

echo "==> Waiting for HA to answer on http://localhost:18123/"
for _ in $(seq 1 60); do
    code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:18123/ || echo 000)
    [ "$code" != "000" ] && break
    sleep 1
done
if [ "$code" = "000" ]; then
    echo "HA did not come up within 60s — check 'docker compose logs homeassistant'" >&2
    exit 1
fi

echo "==> Seeding HA (onboarding + MQTT integration)"
# --build for the same reason as the other two services: seed_ha.py is COPYed
# into its image, and `docker compose run` reuses whatever image already
# exists, so an edit to the seeder is otherwise silently ignored. Hit for real
# while adapting the seeder to a newer HA release: the fix was in the file and
# the container kept running the old code.
docker compose run --build --rm ha-seed

echo "==> Starting the bridge (rebuilding from the repo's real Dockerfile)"
# --build is not optional: `docker compose up` alone only builds an image
# the first time a service has never been built, and silently reuses
# whatever image already exists on every run after that — even when app/
# or translations/ have since changed. Confirmed as a real, dated bug here:
# every e2e run for two days quietly tested a two-day-stale image instead
# of the code actually being worked on, with no error or warning of any
# kind. Rebuilding is cheap when nothing changed (Docker's own layer cache
# still applies), so there's no real cost to always doing it.
docker compose up -d --build bridge
echo "==> Waiting for 'Bridge ready' in bridge logs (up to 120s)"
# Deliberately NOT `docker logs -f | grep -qm1 ...`: grep -q exits as soon as
# it sees a match, closing the pipe early and sending SIGPIPE upstream to
# `docker logs -f`, which then also exits non-zero — under `set -o
# pipefail` that collides with a bash 3.2 quirk (macOS's stock /bin/bash)
# where PIPESTATUS collapses to a single element whenever the pipeline's
# aggregate status is non-zero, even though grep's own match succeeded.
# Polling non-follow `docker logs` in a loop instead sidesteps all of that.
bridge_ready=0
for _ in $(seq 1 60); do
    if docker logs nibe-e2e-bridge 2>&1 | grep -q "Bridge ready"; then
        bridge_ready=1
        break
    fi
    sleep 2
done
if [ "$bridge_ready" -ne 1 ]; then
    echo "Bridge did not report ready within 120s — check 'docker compose logs bridge'" >&2
    exit 1
fi

# HA only picks up new files under /config/www (the card JS the bridge just
# copied there) on (re)start.
echo "==> Restarting HA once so it picks up the card JS"
docker restart nibe-e2e-homeassistant >/dev/null
# Wait for HA to actually answer again on the host port, the same way the
# initial bring-up does, rather than assuming a fixed sleep is enough. A
# fixed sleep cannot tell "HA is still starting" from "the host port is not
# reachable at all", and the second case is real: Colima's port forwarder
# has been seen to drop every published port mid-run, after the initial
# bring-up check had already passed. Playwright then failed *every* spec
# instantly with ERR_CONNECTION_REFUSED — a whole run reported as nine
# failures with nothing to do with the code under test. Polling here turns
# that into one clear error naming the actual problem.
ha_back=0
for _ in $(seq 1 60); do
    code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:18123/ || echo 000)
    if [ "$code" != "000" ]; then
        ha_back=1
        break
    fi
    sleep 1
done
if [ "$ha_back" -ne 1 ]; then
    echo "HA did not come back within 60s of the restart — check 'docker compose logs homeassistant'" >&2
    exit 1
fi

echo "==> Waiting for seed-out/credentials.json to appear on the host"
# ha-seed writes these inside its bind-mounted /seed-out just before exiting,
# but on this host's virtiofs (Colima) the write can take a moment to become
# visible through the bind mount after the container exits — seen in
# practice as a spurious ENOENT from the Playwright test's readCredentials()
# immediately after ha-seed reported "done". Poll for it rather than assume
# it's already there.
seed_ready=0
for _ in $(seq 1 30); do
    if [ -f seed-out/credentials.json ] && [ -f seed-out/token.txt ]; then
        seed_ready=1
        break
    fi
    sleep 1
done
if [ "$seed_ready" -ne 1 ]; then
    echo "seed-out/credentials.json never appeared on the host — check the ha-seed step" >&2
    exit 1
fi

echo "==> Waiting for HA to report state RUNNING"
# Answering the port is necessary but nowhere near sufficient, and a fixed
# sleep is not either. HA accepts TCP connections well before it has finished
# starting, and requests made in that window fail in ways that look like
# anything but a warm-up problem: `net::ERR_EMPTY_RESPONSE`, `socket hang up`,
# or a locator that simply never resolves. A fixed 20s covered it on HA
# 2024.10 and did not on 2026.9 — the first four specs failed with three
# different errors while the fifth onward passed, which reads as four
# unrelated bugs rather than one slow startup.
#
# /api/config reports `"state": "RUNNING"` only once startup is complete, so
# gate on that instead of on the clock. The token written by ha-seed survives
# the restart.
ha_running=0
for _ in $(seq 1 90); do
    # `|| true` is not optional: this script runs under `set -o pipefail`,
    # and while HA is still starting curl exits 52 ("empty reply from
    # server") — precisely the condition being waited out. Without it the
    # failed pipeline trips `set -e` and aborts the whole run on the first
    # poll, which is what happened the first time this loop was written.
    state=$({ curl -s -H "Authorization: Bearer $(cat seed-out/token.txt)" \
        http://localhost:18123/api/config 2>/dev/null || true; } |
        sed -n 's/.*"state": *"\([A-Z_]*\)".*/\1/p')
    if [ "$state" = "RUNNING" ]; then
        ha_running=1
        break
    fi
    sleep 2
done
if [ "$ha_running" -ne 1 ]; then
    echo "HA never reported state RUNNING within 180s — check 'docker compose logs homeassistant'" >&2
    exit 1
fi

echo "==> Running the Playwright test"
set +e
HA_URL=http://localhost:18123 npx playwright test
TEST_EXIT=$?
set -e

if [ "$KEEP_OPEN" -eq 1 ]; then
    echo "==> --keep-open set: leaving the stack up. Tear down later with: ./run.sh --down"
else
    teardown
fi

exit "$TEST_EXIT"
