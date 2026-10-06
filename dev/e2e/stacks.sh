#!/usr/bin/env bash
# Extra harness stacks, for running the e2e suite in parallel.
#
#   ./stacks.sh up [N]    Bring up stacks B…(N-1) next to run.sh's stack A
#                         (default N=3: B and C), each from a clean slate.
#   ./stacks.sh down      Tear down every extra stack (stack A is left alone).
#   ./stacks.sh status    Show which stacks are up.
#
# Then run the suite on all of them, one Playwright worker per stack:
#   E2E_STACKS=3 npx playwright test
#
# Stack A is the one run.sh brings up (bring it up with ./run.sh --keep-open
# first). Stack i is compose project nibe-e2e-<b|c|…> with host ports
# i*10000 above stack A's (HA 28123, broker 28830, mock API 28443 for B) and
# its own seed-out-<letter>/ — see tests/support/stacks.ts, which the specs
# use to find their stack. The images are shared with stack A. Each stack
# needs roughly 0.5 GB of Docker memory.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

LETTERS=abcdefgh

stack_env() {
    local i=$1 letter=${LETTERS:$1:1}
    export COMPOSE_PROJECT_NAME="nibe-e2e-$letter"
    export E2E_PREFIX="nibe-e2e-$letter"
    export HA_PORT=$((18123 + i * 10000))
    export MQTT_PORT=$((18830 + i * 10000))
    export MOCK_PORT=$((18443 + i * 10000))
    export SEED_OUT="./seed-out-$letter"
}

wait_for_ha_port() {
    local code=000
    for _ in $(seq 1 90); do
        code=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$HA_PORT/" || echo 000)
        [ "$code" != "000" ] && return 0
        sleep 1
    done
    echo "[$E2E_PREFIX] HA did not answer on port $HA_PORT" >&2
    return 1
}

up_one() {
    stack_env "$1"
    local log="$E2E_PREFIX"
    echo "[$log] starting from a clean slate"
    docker compose down -v >/dev/null 2>&1 || true
    mkdir -p "$SEED_OUT"
    rm -f "$SEED_OUT"/*.json "$SEED_OUT"/*.txt
    docker compose up -d mosquitto mock-nibe-api homeassistant >/dev/null 2>&1
    wait_for_ha_port
    echo "[$log] seeding HA"
    docker compose run --rm ha-seed >/dev/null 2>&1
    docker compose up -d --no-deps supervisor >/dev/null 2>&1
    local since
    since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    docker compose up -d --no-deps bridge >/dev/null 2>&1
    local ready=0
    for _ in $(seq 1 90); do
        if docker logs --since "$since" "$E2E_PREFIX-bridge" 2>&1 | grep -q "Bridge ready"; then
            ready=1
            break
        fi
        sleep 2
    done
    if [ "$ready" -ne 1 ]; then
        echo "[$log] bridge did not report ready" >&2
        return 1
    fi
    # As run.sh: restart HA once so it picks up the card JS the bridge installed.
    docker restart "$E2E_PREFIX-homeassistant" >/dev/null
    wait_for_ha_port
    local state=""
    for _ in $(seq 1 90); do
        state=$({ curl -s -H "Authorization: Bearer $(cat "$SEED_OUT/token.txt")" \
            "http://localhost:$HA_PORT/api/config" 2>/dev/null || true; } |
            sed -n 's/.*"state": *"\([A-Z_]*\)".*/\1/p')
        [ "$state" = "RUNNING" ] && break
        sleep 2
    done
    if [ "$state" != "RUNNING" ]; then
        echo "[$log] HA never reached RUNNING" >&2
        return 1
    fi
    echo "[$log] ready — HA http://localhost:$HA_PORT"
}

down_one() {
    stack_env "$1"
    docker compose down -v >/dev/null 2>&1 || true
    rm -rf "./seed-out-${LETTERS:$1:1}"
    echo "[$E2E_PREFIX] down"
}

cmd=${1:-}
case "$cmd" in
    up)
        n=${2:-3}
        pids=()
        for i in $(seq 1 $((n - 1))); do
            up_one "$i" &
            pids+=($!)
        done
        status=0
        for pid in "${pids[@]}"; do
            wait "$pid" || status=1
        done
        exit "$status"
        ;;
    down)
        for i in $(seq 1 7); do
            letter=${LETTERS:$i:1}
            if docker ps -a --format '{{.Names}}' | grep -q "^nibe-e2e-$letter-"; then
                down_one "$i"
            fi
        done
        ;;
    status)
        docker ps --format '{{.Names}}\t{{.Status}}' | grep '^nibe-e2e' | sort
        ;;
    *)
        sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
        exit 1
        ;;
esac
