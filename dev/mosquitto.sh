#!/bin/bash
# ==============================================================================
# Disposable MQTT broker for development and integration tests
# ==============================================================================
#
# Runs a plain eclipse-mosquitto container, isolated from anything else on the
# machine: a non-default port, no persistence, and `reset` removes it outright.
# It exists so that this bridge's actual wire behaviour (discovery configs,
# retained messages, entity-type-change cleanup) can be verified against a
# real broker rather than only against the mocked client in tests/test_*.py.
# See tests/test_mqtt_broker_integration.py's module docstring for the
# concrete bug (GitHub issue #23) that mocked-only tests could not have
# caught, which is why this exists.
#
# Port 1894 (not 1883, and deliberately not 1893 either — the same pattern
# used by the sibling ha-history-repair repo's own dev/mosquitto.sh, kept on
# a different port so both repos' dev brokers can run at once without
# colliding if this Mac ever has both checked out and running simultaneously,
# which it does).
#
#   ./dev/mosquitto.sh start     start the broker (pulls the image if needed)
#   ./dev/mosquitto.sh stop      stop and remove the container
#   ./dev/mosquitto.sh status    is it running?
#   ./dev/mosquitto.sh reset     alias for stop; there is no persisted state

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTAINER="nibe-dev-mosquitto"
IMAGE="eclipse-mosquitto:2"
PORT="${NIBE_DEV_MQTT_PORT:-1894}"

is_running() {
    docker ps --format '{{.Names}}' | grep -qx "${CONTAINER}"
}

cmd_start() {
    if is_running; then
        echo "Already running on port ${PORT}."
        return 0
    fi
    docker rm -f "${CONTAINER}" > /dev/null 2>&1 || true

    echo "Starting mosquitto on port ${PORT}…"
    docker run -d --name "${CONTAINER}" \
        -p "127.0.0.1:${PORT}:1894" \
        -v "${REPO}/dev/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro" \
        "${IMAGE}" > /dev/null

    for _ in $(seq 1 40); do
        if docker exec "${CONTAINER}" sh -c "mosquitto_pub -p 1894 -t healthcheck -m ok" \
            > /dev/null 2>&1; then
            break
        fi
        sleep 0.25
    done

    if ! docker exec "${CONTAINER}" sh -c "mosquitto_pub -p 1894 -t healthcheck -m ok" \
        > /dev/null 2>&1; then
        echo "mosquitto failed to start. Container logs:" >&2
        docker logs "${CONTAINER}" >&2
        exit 1
    fi

    echo "Ready: 127.0.0.1:${PORT}"
}

cmd_stop() {
    if ! is_running; then
        echo "Not running."
        return 0
    fi
    echo "Stopping mosquitto…"
    docker rm -f "${CONTAINER}" > /dev/null
    echo "Stopped."
}

cmd_status() {
    if is_running; then
        echo "Running on port ${PORT}."
    else
        echo "Not running."
    fi
}

case "${1:-}" in
    start)  cmd_start ;;
    stop)   cmd_stop ;;
    status) cmd_status ;;
    reset)  cmd_stop ;;
    *)
        echo "usage: $0 {start|stop|status|reset}" >&2
        exit 64
        ;;
esac
