#!/bin/bash
# ==============================================================================
# Home Assistant Add-on: Nibe S-Series MQTT Bridge
# ==============================================================================
#
# Reads log level and mode from options.json and passes them as CLI arguments
# to the Python bridge. All other configuration is read directly from
# /data/options.json by load_config() in generate_nibe_mqtt.py.
#
# SVG assets are copied to /config/www/ here so they are available to the
# Lovelace frontend. The Lovelace card file itself is copied and registered
# by the Python bridge at startup via _copy_card_file().

set -euo pipefail

MODE=$(jq -r '.mode // "essential"' /data/options.json 2>/dev/null || echo "essential")
LOG_LEVEL=$(jq -r '.log_level // "info"' /data/options.json 2>/dev/null || echo "info")
REMOVE_FRONTEND=$(jq -r '.remove_frontend // false' /data/options.json 2>/dev/null || echo "false")

# ── MQTT auto-discovery via Supervisor Services API ───────────────────────────
# If the Mosquitto broker add-on is installed and running, the Supervisor
# exposes its connection details via the services API. This lets users skip
# manual MQTT configuration entirely. Falls back to options.json values if
# the service is unavailable or returns an error.
if [ -n "${SUPERVISOR_TOKEN:-}" ]; then
    MQTT_SVC=$(curl -sf \
        -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
        http://supervisor/services/mqtt 2>/dev/null || echo "")
    if echo "${MQTT_SVC}" | jq -e '.result == "ok"' > /dev/null 2>&1; then
        SVC_HOST=$(echo "${MQTT_SVC}" | jq -r '.data.host // empty')
        SVC_PORT=$(echo "${MQTT_SVC}" | jq -r '.data.port // empty')
        SVC_USER=$(echo "${MQTT_SVC}" | jq -r '.data.username // empty')
        SVC_PASS=$(echo "${MQTT_SVC}" | jq -r '.data.password // empty')
        if [ -n "${SVC_HOST}" ]; then
            echo "MQTT service discovered via Supervisor: ${SVC_HOST}:${SVC_PORT}"
            export NIBE_MQTT_BROKER="${SVC_HOST}"
            [ -n "${SVC_PORT}" ] && export NIBE_MQTT_PORT="${SVC_PORT}"
            [ -n "${SVC_USER}" ] && export NIBE_MQTT_SVC_USERNAME="${SVC_USER}"
            [ -n "${SVC_PASS}" ] && export NIBE_MQTT_SVC_PASSWORD="${SVC_PASS}"
        fi
    fi
fi

echo "Starting Nibe S-Series MQTT Bridge (log=${LOG_LEVEL}, mode=${MODE})..."

# Export cleanup flag so the Python bridge can act on it at shutdown.
# The bridge only removes the Lovelace dashboard and MQTT retained messages
# when this is exactly "1" — normal restarts are not affected.
if [ "${REMOVE_FRONTEND}" = "true" ]; then
    export NIBE_REMOVE_FRONTEND=1
fi

cd /app
exec python3 generate_nibe_mqtt.py \
    --log-level "${LOG_LEVEL}" \
    --mode      "${MODE}"
