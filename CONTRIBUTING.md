# Contributing to Nibe S-Series MQTT Bridge

Thank you for your interest in contributing. This document covers everything you need to get a working development environment, run the test suite, and submit changes.

Before contributing code, read [ARCHITECTURE.md](ARCHITECTURE.md) to understand the module boundaries, threading model, and the two functions that must not be speculatively refactored.

---

## Table of contents

- [Prerequisites](#prerequisites)
- [Repository layout](#repository-layout)
- [Development environment](#development-environment)
- [Running the test suite](#running-the-test-suite)
- [JavaScript test suite (Entity Manager card)](#javascript-test-suite-entity-manager-card)
- [Static analysis](#static-analysis)
- [Coding conventions](#coding-conventions)
- [Submitting changes](#submitting-changes)
- [Mutation testing](#mutation-testing)

---

## Prerequisites

- Python 3.12 or later
- A working Home Assistant installation with the Mosquitto broker add-on (for live testing)
- A Nibe S-series controller on your local network with the local REST API enabled (Menu 7.5.15)

For running tests only — no Nibe controller or HA installation required.

---

## Repository layout

```
nibe_s_series/          ← add-on content (installed into HA)
  app/                  ← all Python source modules
  tests/                ← test suite (20 files + conftest.py)
  translations/         ← en.yaml, nl.yaml, da.yaml, de.yaml, no.yaml, pl.yaml, sv.yaml
  app/menu_structure.yaml ← Nibe Menus dashboard structure (schema: [docs/menu-structure-schema.md](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/blob/main/docs/menu-structure-schema.md))
  config.yaml           ← add-on manifest
  Dockerfile
  run.sh
  build.yaml
  apparmor.txt
  README.md
  DOCS.md
  SECURITY.md
  CHANGELOG.md
  LICENSE.md
  icon.png / logo.png
docs/                   ← SVG diagrams and screenshots (GitHub display)
repository.json         ← HA add-on store manifest
ARCHITECTURE.md         ← developer reference
CONTRIBUTING.md         ← this file
.gitignore
```

All production Python source lives in `nibe_s_series/app/`. The test suite lives in `nibe_s_series/tests/`. `pytest.ini` sets `pythonpath = app` so tests import modules directly by name without an `app.` prefix.

---

## Development environment

**Shortcut:** steps 2-4 below (venv, dependencies, verification), plus the
JS test tooling in `app/` and `dev/e2e/`, the pre-commit hook, and a check
for Docker/Colima/`gh` CLI, are all handled by:

```bash
./dev/setup.sh          # set up / update everything
./dev/setup.sh --check  # report what's missing without installing anything
./dev/setup.sh --doctor # run lint + type-check + both test suites for real
```

Safe to re-run any time — see its own header comment for details. The
manual walkthrough below is what it automates, useful if you want to
understand or do these steps individually.

**1. Clone the repository**

```bash
git clone https://github.com/whatsinabyte/nibe-smo-mqtt-bridge.git
cd nibe-smo-mqtt-bridge/nibe_s_series
```

**2. Create a virtual environment**

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
```

**3. Install dependencies**

```bash
pip install -r requirements-dev.txt
```

`requirements-dev.txt` installs runtime dependencies, the full test stack, and all static analysis tools. It is a superset of `requirements.txt` and `requirements-test.txt`.

**4. Verify the setup**

```bash
pytest tests/ --cov=app --cov-report=term-missing -q
```

All tests should pass. The suite runs in approximately 5 minutes on a modern machine.

---

## Running the test suite

**Standard run** (uses Hypothesis `ci` profile — 20 examples per property test):

```bash
pytest tests/ --cov=app --cov-report=term-missing
```

**Thorough run** (500 examples per property test):

```bash
HYPOTHESIS_PROFILE=thorough pytest tests/ --cov=app --cov-report=term-missing
```

**Parallel run** (requires pytest-xdist):

```bash
pytest tests/ -n auto --dist=loadscope --cov=app --cov-report=term-missing
```

`--dist=loadscope` is required, not optional: several test classes in
`test_entity_manager_snapshots.py` share one hardcoded `/tmp/...` path
across all their own test methods, and xdist's default distribution can
send two tests from the same class to different worker processes, which
then race on that shared file. `--dist=loadscope` keeps same-class tests
on one worker, avoiding the race. Confirmed flake-free across repeated
runs with the flag; without it, the default distribution reproducibly
failed different tests in that file on 3 of 4 runs.

**Replay a specific Hypothesis failure** — when a test fails, Hypothesis prints a `--randomly-seed` value. Replay with:

```bash
pytest tests/test_entity_manager.py --randomly-seed=<seed>
```

**Single test file:**

```bash
pytest tests/test_entity_detection.py -v
```

### Test file ownership

Each source module has a corresponding test file:

| Source | Test file |
|---|---|
| `nibe_api.py` | `test_api.py` |
| `nibe_caching.py` | `test_caching.py` |
| `nibe_discovery_config.py` | `test_mqtt_publisher.py` |
| `nibe_dynamic_map.py` | `test_dynamic_map.py` |
| `nibe_entity_detection.py` | `test_entity_detection.py` |
| `nibe_entity_manager.py` | `test_entity_manager.py`, `test_entity_manager_snapshots.py`, `test_entity_manager_changelog.py`, `test_entity_manager_dynamic.py`, `test_entity_manager_polling.py`, `test_entity_manager_commands.py`, `test_entity_manager_lifecycle.py`, `test_entity_manager_state.py`, `test_entity_manager_discovery.py` |
| `nibe_mqtt_publisher.py` | `test_mqtt_publisher.py` |
| `nibe_ha_integration.py` | `test_ha_integration.py` |
| `nibe_lovelace.py` | `test_lovelace.py` |
| `nibe_test_runner.py` | `test_ha_integration.py` |
| `generate_nibe_mqtt.py` | `test_generate.py` |
| `nibe_utils.py` | `test_utils.py` |
| Card JS logic | `test_card.py` |

Real-infrastructure integration suites (not tied to a single source
module — see their own sections below): `test_api_integration.py`,
`test_mqtt_broker_integration.py`, `test_ha_supervisor_integration.py`,
`test_filesystem_integration.py`, `test_end_to_end_startup.py`.

`test_entity_manager.py` was split into 9 files as it grew unwieldy (~9,800 lines). The remaining `test_entity_manager.py` holds shared test utilities and base classes used by the other 8; the rest are grouped by subsystem (snapshots, changelog, dynamic points, polling, commands, lifecycle, state, discovery). New `EntityManager` tests go in whichever of the 9 files matches their subsystem — create a new file only if a subsystem genuinely doesn't fit any existing one, not as a default.

### Critical constraints

- **`@freeze_time` is forbidden in `test_entity_manager.py`** — it causes xdist worker contamination of iterator-based `time.time` mocks. Use `patch('nibe_entity_manager.time.time', return_value=...)` instead.
- **All test paths must patch `notify_ha` and `dismiss_ha`** — live calls during test runs create persistent HA notifications. The `_trigger_and_wait` helper patches these by default.
- **Create fresh `EntityManager` instances inside `@given` tests** — `setUp` runs once per method, not once per Hypothesis example.
- **`database=None` is required in all Hypothesis profiles** — prevents `FlakyStrategyDefinition` errors from non-deterministic Unicode surrogate hashing on Python 3.12+.

### MQTT broker integration tests

`tests/test_mqtt_broker_integration.py` runs the discovery/cleanup logic
against a real MQTT broker instead of a mocked client. It exists because a
mock's message delivery order is whatever the test script says it is — a
real broker's is not, and that gap is exactly what let
[GitHub issue #23](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/issues/23)
through undetected by the rest of the (mocked) suite. See the module's own
docstring for the full reasoning.

Skipped by default — every other test file still runs with a plain `pytest`
invocation. To run it:

```bash
./dev/mosquitto.sh start   # disposable, isolated eclipse-mosquitto container
NIBE_MQTT_TEST_HOST=127.0.0.1 NIBE_MQTT_TEST_PORT=1894 \
  pytest tests/test_mqtt_broker_integration.py
./dev/mosquitto.sh stop
```

Never point `NIBE_MQTT_TEST_HOST` at a broker serving a real Home Assistant
instance — this suite publishes real (retained) discovery configs on it.

`TestBrokerRestartAgainstARealBroker` (opt-in via
`NIBE_MQTT_TEST_ALLOW_BROKER_RESTART=1`) actually restarts the shared dev
broker container. If running this file together with other suites under
`-n auto`, use `--dist=loadfile` (not `--dist=loadscope`) — loadscope only
keeps one *class's* tests on the same worker, but the broker is shared at
the *file* level, so a different class's tests can still land on another
worker and race the restart. Confirmed empirically: this combination
flaked under `--dist=loadscope` (2-3 of 4 full-suite runs) and was
completely stable across repeated runs once switched to `--dist=loadfile`.

### Nibe REST API integration tests

`tests/test_api_integration.py` runs `NibeApiClient` against a real
`http.server` instance on an ephemeral localhost port — proving the retry
logic, error handling, and body reading behave correctly over a real
socket, not just that the right mocked `urlopen` calls happen. Unlike the
MQTT broker suite above, this needs no external service or setup: it's
part of the normal, always-run suite. Response codes and payload shapes
are checked against the vendor's own Local REST API documentation, not
just against what `nibe_api.py`'s own code assumes — this is what caught
`reset_notifications()`/`write_device_mode()` using their own separate
`urlopen` calls (204/405/500 and 400/401/403 respectively) with no
coverage at all until this suite added it.

`TestRealMisbehaviourAgainstARealServer` goes further than vanilla/
documented-happy-path behaviour: it proves `self._lock` actually
serializes concurrent requests on a real socket (the real controller has
been observed, per community reports, to stop responding under
overlapping request load — this is the whole reason that lock exists),
that a malformed/truncated JSON body is handled gracefully rather than
crashing, and that a real connection refusal (device rebooting, API not
yet up) is retried and reported like any other transient failure. It also
proves `ssl_context` is genuinely wired into `urlopen()`'s TLS handshake
against a real self-signed certificate (both rejecting an untrusted one
and accepting one signed by a trusted CA), that a server which actually
inspects the `Authorization` header value — not a scripted 401 — is
handled correctly, and that a large, genuinely `Transfer-Encoding:
chunked` `/points` response decodes correctly end to end. None of these
are exercisable through a mocked `urlopen`.

### HA Supervisor integration tests

`tests/test_ha_supervisor_integration.py` runs `notify_ha`/`dismiss_ha`
and `HAEntityRegistryWatcher` against a real `http.server` (for the REST
calls) plus a hand-built WebSocket server implementing the RFC 6455
handshake and frame (un)masking by hand — no WebSocket *server* library
was available in the venv, only the client-side `websocket-client` this
project already depends on. Message shapes are checked against the
official [HA WebSocket API docs](https://developers.home-assistant.io/docs/api/websocket/),
not just against what `nibe_ha_integration.py`'s own code assumes.

No external service or opt-in env var needed — part of the normal,
always-run suite. Covers real misbehaviour on both sides: auth rejection,
a connection dropped mid-handshake, a malformed greeting, a missing
app-level pong triggering reconnect, a real RFC 6455 protocol-level ping
frame (opcode 0x9) from the server getting a real pong back without
disrupting the event stream, `notify_ha` genuinely timing out (not
hanging forever) against a server that accepts the connection and then
goes silent, and a debounced `refresh_registry()` call racing a real
reconnect on two concurrent connections to the same stub server.

### Filesystem integration tests

`tests/test_filesystem_integration.py` proves the recovery logic around
`wanted_points.json`, `dynamic_point_map.json`, and `menu_structure.yaml`
against real filesystem failures — a truncated file (crash mid-write), a
permission-denied directory, and (via `RLIMIT_FSIZE` + ignoring
`SIGXFSZ`) a real `OSError` from the OS reproducing what a genuinely full
disk does — not a mocked `open()` that only ever fails in the shape a
test tells it to. No external service or opt-in env var needed.

### End-to-end startup test

`tests/test_end_to_end_startup.py` drives `_build_infrastructure()` +
`_run_startup_sequence()` + `_shutdown()` directly against real stub
servers for all three external interfaces at once (Nibe API, MQTT broker,
HA Supervisor REST + WebSocket) plus a real filesystem, then confirms a
real point from the real Nibe stub was discovered, classified, and its
discovery config is readable as a real retained message by an independent
subscriber on the real broker. Every other integration suite above proves
one interface's failure handling in isolation; this is the only one that
proves the three actually interact correctly during a real startup.
Deliberately does not test the Lovelace card's own JavaScript — that has
its own, separate test suite under `app/tests-js/` (see
[JavaScript test suite (Entity Manager card)](#javascript-test-suite-entity-manager-card)
below); this suite only proves the bridge's side of the
`docs/card-api.md` MQTT contract the card depends on.

Skipped unless `NIBE_MQTT_TEST_HOST` is set, same as the MQTT broker
suite. **Must not** be run concurrently with `test_mqtt_broker_integration.py`
(or another instance of itself) against the same broker — it calls the
real, unscoped `scan_mqtt_discovery()`, which subscribes to the wildcard
`homeassistant/+/+/config` across the *entire* broker, so it can pick up
another suite's own in-flight retained topics and flake. See the module's
own docstring for the full reasoning.

---

## JavaScript test suite (Entity Manager card)

`app/nibe-entity-manager-card.js` — the Lovelace custom card — has its own
test tooling, entirely separate from the Python `pytest` suite above: a
`package.json` scoped to `app/`, so it never touches the repo-root
`requirements*.txt`/`pytest.ini` and can't accidentally get pulled into a
Python dependency install.

**Prerequisites:** Node.js 18+ (for the built-in `node:stream/web`
`DecompressionStream`/`CompressionStream` used by the gzip test coverage)
and `npm`.

**1. Install dependencies** (once, from `app/`):

```bash
cd app
npm install
```

**2. Run the primary suite** — Vitest + jsdom, instantiating the real
`<nibe-entity-manager-card>` custom element against a fake `hass` object
that records MQTT subscribe/publish calls (`app/tests-js/support/fake-hass.js`)
and drives it through fixtures built from `docs/card-api.md`
(`app/tests-js/support/fixtures.js`):

```bash
npm test
```

Watch mode while iterating: `npm run test:watch`. Coverage report:
`npm run test:coverage` — scoped to `nibe-entity-manager-card.js` itself
(test support files excluded) and enforced via thresholds in
`app/vitest.config.js` (90% statements/lines, 80% functions, 75% branches);
the command exits non-zero if coverage drops below them.

**3. Run the Playwright smoke suite** — a handful of tests for what jsdom
can't verify: real CSS media-query-driven responsive layout (the 600px
desktop/mobile breakpoint), real pointer/click behaviour, and modal
visibility as actually laid out by a browser. Loads the card through the
static fixture page `app/tests-js/e2e/fixture.html` (a stub `hass` injected
via `<script type="module">`, no real Home Assistant needed):

```bash
npm run test:e2e:install   # once, downloads the Chromium browser
npm run test:e2e
```

**What's covered where:** every inbound MQTT topic's happy path plus
malformed/missing/empty/null payload handling, gzip decompression
round-tripping, the Fuse.js CDN-load fallback to substring search, every
outbound publish (enable/disable, snapshot save/restore/delete, mark-read)
asserted against the exact topic and payload shape in `docs/card-api.md`,
and all filter/sort/pagination/selection logic live in Vitest. Responsive
layout switching and real click/modal behaviour live in the Playwright
smoke suite. Neither suite modifies `nibe-entity-manager-card.js` itself —
see the suite's own test files for any testability findings.

**Linting:** ESLint (flat config, `app/eslint.config.js`), the JS-side
equivalent of ruff — run it before submitting a PR:

```bash
npm run lint
```

Three scopes, each with the global set that actually applies to it: the
card file itself (browser globals only — no Node.js globals may appear
there, since it runs inside Home Assistant's frontend), the Vitest suite
(Node + browser, since Vitest drives jsdom), and the Playwright suite
(Node, plus browser globals in the top-level spec file since
`page.evaluate()` callbacks are serialised into the browser page inline).

**CI:** lint and both test suites run on every push/PR to `main` in the
`js-tests` job of `.github/workflows/tests.yml`, independent of the Python
`pytest` job.

**Real end-to-end harness:** `dev/e2e/` is a separate, heavier-weight,
manual-only harness — a real Home Assistant instance, a real Mosquitto
broker, and the actual bridge container (built from this repo's own
`Dockerfile`) via Docker Compose, with one Playwright test driving a real
browser through the real card. It proves the one thing the suites above
structurally can't: that the real HA frontend's `hass.connection`/
`hass.callService` actually agrees with `app/tests-js/support/fake-hass.js`'s
approximation of it. Not wired into CI — see `dev/e2e/README.md` for how
to run it.

---

## Static analysis

All five tools must pass clean before submitting a PR.

**Ruff** (linting and formatting):

```bash
ruff check app/ tests/
ruff format --check app/ tests/
```

**Mypy** (type checking):

```bash
mypy app/
```

**Vulture** (dead code):

```bash
vulture --exclude node_modules app/ vulture_whitelist.py
```

`vulture_whitelist.py` documents the small number of known false positives —
symbols only ever exercised from `tests/`, which a plain `vulture app/` scan
can't see. Regenerate it with `vulture app/ --make-whitelist` after
confirming a new finding is a genuine false positive, not real dead code.

**Bandit** (security):

```bash
bandit -r app/
```

Fix all findings before submitting. If a finding is a false positive, add a `# noqa` or `# nosec` comment with a brief explanation of why.

**ShellCheck** (shell scripts):

```bash
shellcheck run.sh run-mutmut.sh dev/setup.sh dev/mosquitto.sh dev/e2e/run.sh
```

Not installed via pip/npm — MacPorts: `sudo port install shellcheck` (or your platform's equivalent). This repo's shell scripts target bash 3.2 (macOS's shipped version, not a newer one from a package manager), and this catches the class of bug that assumption creates — e.g. a negative array index (`${arr[-1]}`, needs bash 4.3+) that would otherwise fail silently and fast on a contributor's Mac with no error message pointing at why.

---

## Coding conventions

**Module boundaries** — each module has a documented public surface and a documented "what this module does NOT do" section. Respect these boundaries. If a change requires crossing a boundary, reconsider the design first.

**No backwards compatibility** — this project has no installed user base that requires API stability. Remove dead code immediately. Rename freely if the new name is clearer.

**Pure functions in `nibe_entity_detection.py`** — this module must remain stateless, I/O-free, and import-free from the rest of the bridge. Any new classification logic goes here as pure functions.

**MQTT topic strings** — all topic strings are defined in `MgmtTopic` and `BrowserTopic` enums in `nibe_mqtt_publisher.py`. Never construct a topic string outside this module.

**Do not speculatively refactor `_fetch_bulk_data` or `_publish_dynamic_changes`** — these functions are intentionally large. Their complexity is inherent to the algorithm, they are fully covered by tests, and there is no current bug justifying the refactor risk. See `ARCHITECTURE.md` section 4.4 for the full rationale.

**Hypothesis `@example` pins** — property-based tests should include `@example` decorators grounding the test in real firmware observations where applicable. This documents why the test exists and ensures the specific case is always exercised regardless of the random seed.

**`optimistic: false`** — all writable MQTT discovery configs (switch, select, number) must include `"optimistic": False`. Missing this causes the HA UI to flip back to the old value during the write confirmation window.

---

## Submitting changes

1. Fork the repository and create a branch from `main`
2. Make your changes
3. Run the full test suite and confirm all tests pass
4. Run all four static analysis tools and confirm clean output
5. Add or update tests for any changed behaviour — aim to maintain existing coverage
6. Update `CHANGELOG.md` under `[Unreleased]` with a brief description of the change
7. Open a pull request against `main` with a clear description of what changed and why

For significant changes — new features, architectural changes, new entity type support — open a GitHub Discussion first to align on approach before writing code.

For changes to `nibe-entity-manager-card.js`, consult [`docs/card-api.md`](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/blob/main/docs/card-api.md) for the full MQTT protocol the card depends on.

---

## Mutation testing

Mutation testing is used periodically to identify gaps in the test suite, not as a continuous process. The infrastructure is in place if you want to run it.

**Phases and their status:**

| Phase | Target | Status |
|---|---|---|
| 1 | `nibe_mqtt_publisher.py`, `nibe_discovery_config.py` | Ceiling reached |
| 2 | `nibe_entity_detection.py`, `nibe_dynamic_map.py`, `nibe_api.py` | Ceiling reached |
| 3 | `nibe_entity_manager.py` | Ceiling reached — full file, 4,439 mutants, run on a local Mac copy (not the ODROID) rather than the CI target |
| 4 | `nibe_ha_integration.py`, `nibe_lovelace.py`, `nibe_caching.py`, `nibe_test_runner.py`, `nibe_utils.py`, `generate_nibe_mqtt.py` | Ceiling reached |

All four phases have now been run at least once and their survivors worked down to the structural ceiling (log format strings, genuine semantic equivalents). Re-running a phase after significant changes to its target modules is reasonable to catch newly-introduced gaps, but nothing is currently parked.

**To run any phase** (from the `nibe_s_series/` directory):

```bash
cd ..   # repo root
./run-mutmut.sh 1   # 1, 2, 3, or 4
```

Phase 3 in particular is a multi-hour run — mutmut's own default worker count (one per CPU core) can exhaust memory on machines with limited RAM when each worker runs the full `test_entity_manager*` suite in parallel. Set `MUTMUT_MAX_CHILDREN` to cap concurrency if you hit this, e.g. `MUTMUT_MAX_CHILDREN=2 ./run-mutmut.sh 3`.

**mutmut 3.x limitations to be aware of:**
- `only_mutate` uses `fnmatch` against file paths only — function-level scoping (`::function_name*`) generates 0 mutants silently
- Pragmas on the closing `)` of multi-line log calls do NOT suppress mutations of string literals on inner lines — these survivors are at the structural ceiling
- Pragma syntax: `# pragma: no mutate` (space required) on the **closing `)` line**

Survivors that are genuine semantic equivalents (log format strings, equivalent boolean expressions) do not need new tests — annotate them with the pragma instead.
