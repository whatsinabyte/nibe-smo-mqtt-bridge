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
    echo "This is gitignored, developer-local reference data — see the top-level CLAUDE.md." >&2
    exit 1
fi

# Always start from a clean slate — reusing a stopped/restarted ha-config
# volume is not supported (see the idempotency note above and README.md).
echo "==> Starting from a clean slate"
docker compose down -v >/dev/null 2>&1 || true
rm -f seed-out/*.json seed-out/*.txt

echo "==> Bringing up mosquitto, mock-nibe-api, homeassistant"
docker compose up -d mosquitto mock-nibe-api homeassistant

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
docker compose run --rm ha-seed

echo "==> Starting the bridge (builds from the repo's real Dockerfile on first run)"
docker compose up -d bridge
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
# 15s was occasionally too short in practice: whichever spec file happens to
# run first (Playwright runs specs in a fixed order, alphabetical by
# filename) can hit HA before its frontend/dashboard config has fully
# reloaded post-restart, and never finds nibe-entity-manager-card in time —
# a later spec in the same run passes fine, having gotten a few more
# seconds of warm-up "for free". 25s reliably clears that race.
sleep 25

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
