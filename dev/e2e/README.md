# Real-stack end-to-end harness (manual, on-demand — not CI)

This directory is a throwaway dev harness that proves the full loop actually
works: a real Home Assistant instance, a real Mosquitto broker, the actual
bridge built from this repo's own `Dockerfile`, and the actual Lovelace card
(`app/nibe-entity-manager-card.js`) loaded through HA's real frontend in a
real Chromium browser driven by Playwright.

It is **not** wired into CI and is never run automatically. Nothing here
touches `app/tests-js/` or `.github/workflows/tests.yml`.

## What this proves that the other two suites don't

- The pytest suite's MQTT-broker integration tests (`test_mqtt_broker_integration.py`)
  exercise the bridge against a real broker, but there is no real Home
  Assistant frontend involved — nothing verifies that a browser's real
  `hass.connection` / `hass.callService` actually behaves the way the card
  assumes.
- The card's own Vitest/Playwright suites drive the *real* card element
  against a *stubbed* `hass` object (`app/tests-js/support/fake-hass.js`) —
  thorough for the card's own logic, but the stub is still someone's
  approximation of what real HA does.
- **This harness is the only place both sides are real at once**: a real
  browser, logged into a real HA instance, clicking a real card, which calls
  the real `hass.callService('mqtt', 'publish', ...)`, which round-trips
  through a real Mosquitto broker to the real bridge process (built from
  this repo's actual `Dockerfile`), which really enables a point and
  publishes a real MQTT discovery config — and the test then confirms,
  via HA's own REST API, that a genuinely new HA entity exists and is not
  `unavailable`.

Scope is deliberately narrow: one happy path (enable one disabled entity,
confirm the corresponding HA entity appears). It is not meant to replace
either existing suite's coverage.

## Prerequisites

- Docker Desktop / a working `docker compose` (this was developed and
  verified against Docker Compose v2 via `docker compose`, using Colima on
  macOS — any Docker Engine with the compose plugin works).
  **Intel Mac / MacPorts Colima note:** MacPorts' `docker` port does not
  ship the v2 `compose` CLI plugin — only the standalone `docker-compose`
  v1.29.2 binary, which cannot parse this directory's `docker-compose.yml`
  (v1 rejects the top-level `name:` key, which is v2-only). Fix: download
  the v2 plugin binary and drop it where the Docker CLI looks for plugins:
  ```bash
  mkdir -p ~/.docker/cli-plugins
  curl -sL -o ~/.docker/cli-plugins/docker-compose \
    "https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-darwin-x86_64"
  chmod +x ~/.docker/cli-plugins/docker-compose
  docker compose version   # should now print v2.29.7
  ```
  After that, `docker compose` (space syntax, as used throughout `run.sh`
  and this README) works unmodified — no changes needed to `run.sh` itself.
- Node.js (for Playwright). From the repo's `dev/e2e/` directory:
  ```bash
  npm install
  npx playwright install --with-deps chromium
  ```
- `reference-dumps/all_points_en.json` must exist at the repo root (it's
  gitignored, developer-local reference data — see the top-level
  `CLAUDE.md`). If your worktree doesn't have it, copy it from the main
  checkout at `/Users/marcel/nibe-smo-mqtt-bridge-local/reference-dumps/`.

## What's in here

| Path | Purpose |
|---|---|
| `run.sh` | One-command runner — wraps everything below into a single script. `./run.sh -h` for usage. |
| `docker-compose.yml` | Brings up mosquitto, the mock Nibe API, HA, the one-shot HA seeder, and the bridge itself (built from the repo's real `Dockerfile`). |
| `mock-api/` | Minimal stdlib-only HTTPS server replaying `reference-dumps/all_points_en.json` in the exact shapes `app/nibe_api.py` expects (self-signed TLS, any Basic auth accepted). Also exposes a test-only `POST /mock-control/points/{id}` control channel (host port `18443`) so tests can simulate a firmware value change across polls — not part of the real Nibe API surface. |
| `mosquitto/mosquitto.conf` | Anonymous-auth Mosquitto config — dev only. |
| `bridge/options.json` | Mounted at `/data/options.json` — the standard HA add-on config path `load_config()` reads first; points the bridge at the mock API and the compose-network broker. |
| `ha-seed/` | `configuration.yaml` (+ YAML-mode Lovelace dashboards) and `seed_ha.py`, a one-shot container that headlessly onboards HA and configures the MQTT integration via HA's real REST APIs. |
| `tests/enable-entity.spec.ts` | Happy path: enable one disabled entity, confirm it appears in real HA. |
| `tests/binary-sensor-reclassification.spec.ts` | Enables the first working candidate from a list of genuinely auto-detected binary_sensor points, then uses the mock API's control channel to change its raw value to a non-boolean one, and confirms via HA's own REST API that the old `binary_sensor.nibe_*` entity disappears and a new, available `sensor.nibe_*` entity takes its place — proving `nibe_entity_manager.py`'s dynamic `_reclassify_binary_sensor` is visible correctly in a real HA entity registry, not just at the MQTT-message level. |

## How the bridge runs without a Supervisor

The bridge is normally a Home Assistant Supervisor add-on. `run.sh` and
`generate_nibe_mqtt.py`'s `load_config()` already have documented dev/Docker
escape hatches for everything **except** two things that are genuinely
Supervisor-only and have no dev-mode override (see "Known gap" below):
Lovelace resource/dashboard auto-provisioning, and the HA base-URL/language
auto-detection convenience (both degrade gracefully — see below).

This harness runs `run.sh` completely unmodified (`CMD ["/run.sh"]` in the
Dockerfile, untouched):

- `SUPERVISOR_TOKEN` is simply never set. `run.sh`'s MQTT
  auto-discovery-via-Supervisor-Services-API block checks
  `[ -n "${SUPERVISOR_TOKEN:-}" ]` and no-ops when absent — harmless.
- Credentials (`nibe_username`/`nibe_password`) and every other option come
  from `/data/options.json` (mounted read-only from `bridge/options.json`),
  exactly like a real add-on install — `load_config()`'s documented
  priority order (`secrets.yaml` < `options.json` < env vars < CLI args)
  is honoured unmodified.
- `apk`/Alpine base image, `jq`, `curl` — all already in the Dockerfile;
  nothing extra was needed.

**No production code was changed.** `app/generate_nibe_mqtt.py`,
`app/nibe_api.py`, `app/nibe-entity-manager-card.js`, `run.sh`, and
`Dockerfile` are all byte-identical to what ships in the add-on.

## Known gap: Lovelace auto-provisioning requires a real Supervisor

`nibe_lovelace.py`'s `provision_lovelace_ui()` (resource registration +
"Nibe Bridge"/"Nibe Menus" dashboard creation) is hard-gated on
`SUPERVISOR_TOKEN` and, when present, opens a WebSocket to
`ws://supervisor/core/websocket` — an endpoint that only exists inside a
real Supervisor's internal Docker network. There is no documented dev/Docker
override for this (unlike `mqtt_host`/`api_host`/etc.), and it is not one of
the things `load_config()`'s env-var escape hatches cover. This matches the
task's own anticipated failure mode.

**This is a real, load-bearing gap, not a workaround needed to avoid**
touching production code — the function correctly and safely no-ops without
a Supervisor (logs `"No SUPERVISOR_TOKEN — skipping Lovelace setup"`, does
not crash, does not affect the rest of startup). Confirmed in this harness's
bridge container logs:
```
No SUPERVISOR_TOKEN — skipping Lovelace setup (running outside HA add-on environment)
No SUPERVISOR_TOKEN — skipping menu dashboard teardown (running outside HA add-on environment)
```
So the harness seeds the card resource + a dashboard itself, entirely
outside the bridge, via `ha-seed/configuration.yaml`'s YAML-mode Lovelace
config (`lovelace: mode: yaml`, `resources:`, `dashboards:`) — no Supervisor,
no WebSocket auth dance required, fully reproducible from a file already
checked into this harness. The bridge's own `_copy_card_file()` (which
copies `nibe-entity-manager-card.js` to `/homeassistant/www/`) needs *only*
the shared config volume, not a Supervisor token, so that part runs for
real, unmodified, exactly as in production.

If a from-scratch dev escape hatch for Lovelace provisioning is ever wanted
(e.g. an `NIBE_HA_WS_URL`/`NIBE_HA_TOKEN` override), that would be the
minimal production-code change to close this gap — not attempted here per
the task's ground rules.

## How HA is brought to a usable, unattended state

Modern Home Assistant (this harness pins `homeassistant/home-assistant:2024.10.1`)
requires interactive onboarding (create the admin user) and an interactive
config-flow walk for the MQTT integration (YAML-configured MQTT brokers were
removed from HA years ago). `ha-seed/seed_ha.py` drives both headlessly
using HA's own public REST APIs — no `.storage` file hand-editing:

1. Polls `/api/onboarding` until HA responds.
2. `POST /api/onboarding/users` to create the admin user, exchanges the
   returned `auth_code` for a bearer token via `/auth/token`.
3. Completes the `core_config`, `analytics`, and `integration` onboarding
   steps (all four are required before the frontend stops redirecting to
   `/onboarding.html`).
4. Drives the **mqtt** integration's real config-flow API
   (`POST /api/config/config_entries/flow` → `POST .../flow/{flow_id}`) to
   add an MQTT config entry pointing at the `mosquitto` service — this is
   the current, supported way to configure MQTT headlessly; hand-writing a
   `core.config_entries` entry into `.storage` was considered and rejected
   as version-fragile and undocumented, per the task's own guidance to
   prefer whichever technique is actually reliable.
5. Writes `seed-out/credentials.json` (username/password) and
   `seed-out/token.txt` (bearer token) to a **bind-mounted** host directory
   so the Playwright test (running on the host, not in a container) can
   read them.

**Idempotency / re-run caveat**: steps already done are skipped on a re-run
against a still-running HA. A re-run against a *restarted* container reusing
the same `ha-config` volume is **not** supported — HA's `user` onboarding
step can only run once per volume, and this harness's default
`homeassistant` auth provider rejects the OAuth2 `password` grant this
script could otherwise fall back to (HA returns HTTP 400 for
`grant_type=password` unless the legacy `legacy_api_password` provider is
explicitly configured, which this harness deliberately does not do — it's
deprecated and not worth the extra config surface for a throwaway harness).
**For a clean re-run, tear down with `docker compose down -v`** (wipes the
`ha-config` volume) rather than reusing state. This is the "run once, then
either fully recreate or persist the volume as-is" fallback the task
anticipated as an acceptable outcome if full from-scratch idempotency proved
too fragile — full onboarding-into-config-flow was in fact scripted
end-to-end successfully; only *repeat* onboarding against a stale volume was
out of scope to chase further.

## How to run it

**One command**, from `dev/e2e/`:

```bash
./run.sh
```

This wraps every step below into one script: installs JS deps + the
Chromium browser on first run, always tears down and recreates the stack
from a clean slate first (see the idempotency note above — reusing a
stopped/restarted `ha-config` volume isn't supported, so every `./run.sh`
run is a genuinely clean one), brings up mosquitto/mock-api/HA, polls until
HA answers, runs the headless seeder, starts the bridge and waits for
"Bridge ready" in its logs, restarts HA once so it picks up the card JS,
runs the Playwright test, then tears the stack down again. Exits with the
test's own exit code.

```bash
./run.sh --keep-open   # skip teardown at the end, to poke around the running stack
./run.sh --down        # teardown only (drops the ha-config volume, clears seed-out/)
./run.sh -h             # usage
```

After a `--keep-open` run, HA is reachable at http://localhost:18123 with
the seeded admin credentials in `ha-seed/seed_ha.py`
(`HA_SEED_USERNAME`/`HA_SEED_PASSWORD` in `docker-compose.yml`).

### What each step actually does (what `run.sh` automates)

If you want to run a step manually — e.g. to poke at one stage without
rerunning the whole thing — this is the sequence `run.sh` performs,
unrolled:

```bash
# One-time: JS deps + browser
npm install
npx playwright install --with-deps chromium

# Bring up mosquitto, the mock API, and HA; wait for HA (~15-30s)
docker compose up -d mosquitto mock-nibe-api homeassistant
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:18123/   # poll until non-000

# Headlessly onboard HA + configure the MQTT integration
docker compose run --rm ha-seed

# Start the bridge (builds from the repo's real Dockerfile the first time)
docker compose up -d bridge
docker logs -f nibe-e2e-bridge   # wait for "Bridge ready — ..."

# HA only picks up new files under /config/www (the card JS the bridge just
# copied there) on (re)start — restart it once after the bridge is up:
docker restart nibe-e2e-homeassistant
sleep 15

# Run the test (HA is published on host port 18123, not 8123, to avoid
# colliding with a real HA instance you may already have running locally)
HA_URL=http://localhost:18123 npx playwright test
```

### Tear down

```bash
./run.sh --down
# ...or equivalently:
docker compose down -v   # -v also drops the ha-config volume — required for a clean re-seed
rm -rf seed-out/*.json seed-out/*.txt
```

### Expected runtime

- Image builds (first run only): ~2-3 minutes (bridge image reuses the repo's
  own Dockerfile and pulls the full dev/test Python dependency set).
- Stack startup + HA onboarding + bridge ready: ~30-45 seconds.
- The Playwright test itself: ~5-10 seconds.

## Verification performed

This harness was actually run, not just written. Confirmed for real, in
order, against a freshly-created `ha-config` volume:

- The mock API serves real firmware data over HTTPS with the exact
  `/api/v1/devices/0{,/points,/points/{id},/notifications}` shapes
  `app/nibe_api.py` expects (cross-checked against Nibe's own
  "Local REST API" spec PDF, not just the Python client code).
- `ha-seed` completed onboarding (`user`, `core_config`, `analytics`,
  `integration`) and the MQTT config-flow (`create_entry`) against a real,
  freshly-started HA container, entirely via REST calls.
- The bridge (built from this repo's actual `Dockerfile`, `run.sh`
  unmodified) started, fetched 1158 real points from the mock API, applied
  `mode: essential` (29 enabled / 1158 total — plenty left disabled for the
  test), and logged `Bridge ready`.
- `_copy_card_file()` copied the real card JS to the shared `/homeassistant/www`
  volume with no Supervisor token, confirmed served at `/local/nibe-entity-manager-card.js`
  after an HA restart.
- Lovelace auto-provisioning cleanly no-op'd exactly as documented above;
  the YAML-seeded "Nibe Bridge" dashboard rendered the real
  `nibe-entity-manager-card` custom element instead.
- **The Playwright test passed**: logged into the real HA frontend with the
  seeded admin credentials, opened `/nibe-bridge/entity-manager`, clicked
  `Enable` on real disabled points through the real card, and confirmed via
  `GET /api/states` that new HA entities appeared and at least one left
  `unavailable` (a few of these specific registers legitimately carry a
  firmware "sensor not connected" sentinel value in the real dump this mock
  replays — the bridge correctly reports those as unavailable, which is why
  the test tries a handful of candidates rather than asserting on exactly
  one specific point ID).

## Note on `app/tests-js/` (resolved)

This harness was built by an agent working in an isolated git worktree
checked out from the last commit — at that point `app/tests-js/` (the
Vitest+jsdom suite, 307 tests, plus its own Playwright smoke suite) existed
only as *uncommitted* changes in the main working tree, so the worktree
genuinely couldn't see it and reported it as absent. It exists and is
unrelated to this harness: `app/tests-js/` drives the real card element
against a *stubbed* `hass` object (see `app/tests-js/support/fake-hass.js`
and its own README section in `CONTRIBUTING.md`); this directory
(`dev/e2e/`) is the separate, heavier "everything is real" tier described
above, kept as its own standalone Node project (own `package.json`,
`playwright.config.ts`) rather than folded into `app/`'s tooling, since
it has a fundamentally different runtime shape (Docker Compose, a real HA
instance) and is not meant to run in CI.
