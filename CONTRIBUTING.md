# Contributing to Nibe S-Series MQTT Bridge

Thank you for your interest in contributing. This document covers everything you need to get a working development environment, run the test suite, and submit changes.

Before contributing code, read [ARCHITECTURE.md](ARCHITECTURE.md) to understand the module boundaries, threading model, and the two functions that must not be speculatively refactored.

---

## Table of contents

- [Prerequisites](#prerequisites)
- [Repository layout](#repository-layout)
- [Development environment](#development-environment)
- [Running the test suite](#running-the-test-suite)
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
  tests/                ← test suite (10 files + conftest.py)
  translations/         ← en.yaml, nl.yaml
  app/menu_structure.yaml ← Nibe Menus dashboard structure (schema: [docs/menu-structure-schema.md](docs/menu-structure-schema.md))
  config.yaml           ← add-on manifest
  Dockerfile
  run.sh
  build.yaml
  apparmor.txt
  README.md
  DOCS.md
  SECURITY.md
  CHANGELOG.md
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
pytest tests/ -n auto --cov=app --cov-report=term-missing
```

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
| `nibe_dynamic_map.py` | `test_dynamic_map.py` |
| `nibe_entity_detection.py` | `test_entity_detection.py` |
| `nibe_entity_manager.py` | `test_entity_manager.py` |
| `nibe_mqtt_publisher.py` | `test_mqtt_publisher.py` |
| `nibe_ha_integration.py` | `test_ha_integration.py` |
| `nibe_lovelace.py` | `test_lovelace.py` |
| `generate_nibe_mqtt.py` | `test_generate.py` |
| `nibe_utils.py` | `test_utils.py` |
| Card JS logic | `test_card.py` |

New tests go in the file for the module they test. The 10-file split is final — do not create new test files.

### Critical constraints

- **`@freeze_time` is forbidden in `test_entity_manager.py`** — it causes xdist worker contamination of iterator-based `time.time` mocks. Use `patch('nibe_entity_manager.time.time', return_value=...)` instead.
- **All test paths must patch `notify_ha` and `dismiss_ha`** — live calls during test runs create persistent HA notifications. The `_trigger_and_wait` helper patches these by default.
- **Create fresh `EntityManager` instances inside `@given` tests** — `setUp` runs once per method, not once per Hypothesis example.
- **`database=None` is required in all Hypothesis profiles** — prevents `FlakyStrategyDefinition` errors from non-deterministic Unicode surrogate hashing on Python 3.12+.

---

## Static analysis

All four tools must pass clean before submitting a PR.

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
vulture app/
```

**Bandit** (security):

```bash
bandit -r app/
```

Fix all findings before submitting. If a finding is a false positive, add a `# noqa` or `# nosec` comment with a brief explanation of why.

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

For changes to `nibe-entity-manager-card.js`, consult [`docs/card-api.md`](docs/card-api.md) for the full MQTT protocol the card depends on.

---

## Mutation testing

Mutation testing is used periodically to identify gaps in the test suite, not as a continuous process. The infrastructure is in place if you want to run it.

**Phases and their status:**

| Phase | Target | Status |
|---|---|---|
| 1 | `nibe_mqtt_publisher.py` | Ceiling reached — ~77% kill rate |
| 2 | `nibe_entity_detection.py`, `nibe_dynamic_map.py`, `nibe_api.py` | Ceiling reached — ~68% kill rate |
| 3 | `nibe_entity_manager.py` | Parked — estimated 50–80h runtime |
| 4 | `generate_nibe_mqtt.py` | Parked — threading survivors unresolvable |

**To run Phase 1 or 2** (from the `nibe_s_series/` directory):

```bash
cd ..   # repo root
./run-mutmut.sh 1   # or 2
```

**mutmut 3.x limitations to be aware of:**
- `only_mutate` uses `fnmatch` against file paths only — function-level scoping (`::function_name*`) generates 0 mutants silently
- Pragmas on the closing `)` of multi-line log calls do NOT suppress mutations of string literals on inner lines — these survivors are at the structural ceiling
- Pragma syntax: `# pragma: no mutate` (space required) on the **closing `)` line**

Survivors that are genuine semantic equivalents (log format strings, equivalent boolean expressions) do not need new tests — annotate them with the pragma instead.
