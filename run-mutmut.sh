#!/usr/bin/env bash
# run-mutmut.sh — mutation testing runner for the Nibe MQTT Bridge.
#
# Lives at the repo root alongside pyproject.toml and pytest.ini.
# Expects the standard repo layout:
#   app/        — all production Python modules + menu_structure.yaml
#   tests/      — conftest.py + all test_*.py files
#   pytest.ini  — testpaths=tests, pythonpath=app
#
# SOURCE ROOT WORKAROUND
# Mutmut 3.x only recognises '.', 'src/', 'source/' as source roots.
# Our source lives in app/ (on sys.path via pytest.ini pythonpath=app).
# This script copies the target module(s) to the repo root before running
# mutmut. The copy is kept after the run so that mutmut show/results work
# (mutmut show needs the source file to apply patches against). Always edit
# app/<module>.py — the root-level copy is regenerated on every run.
# The sandbox overrides pythonpath to '. app' so both the mutated file
# (at mutants/) and peer modules (at mutants/app/) are importable.
#
# USAGE
#   ./run-mutmut.sh [phase]
#
#   phase 1 (default) — app/nibe_mqtt_publisher.py (~1-2 hr on ODROID-M1)
#   phase 2 — app/nibe_entity_detection.py, nibe_dynamic_map.py, nibe_api.py
#   phase 3 — app/nibe_entity_manager.py high-risk functions (overnight)
#
# RESUMING AN INTERRUPTED RUN
#   mutmut run          — resumes from where it left off
#   mutmut results      — summary table
#   mutmut show <id>    — diff for a specific mutant
#
# INTERPRETING SURVIVORS
#   Add a test that pins the exact value/condition, or annotate the line
#   with  # pragma: nomut  for genuinely equivalent mutations (e.g. log strings).

set -euo pipefail

PHASE="${1:-1}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

stage_module() {
    # stage_module <name> copies app/<name>.py to ./<name>.py
    # The copy persists after the run so that mutmut show/results work.
    # It is a read-only reference copy — always edit app/<name>.py, not this.
    # Re-running run-mutmut.sh will overwrite it with a fresh copy.
    local name="$1"
    local src="$SCRIPT_DIR/app/${name}.py"
    local dst="$SCRIPT_DIR/${name}.py"
    cp "$src" "$dst"
    echo "[mutmut] Staged: ${name}.py (copy of app/${name}.py — kept for mutmut show)"
}

write_phase_1_config() {
cat > "$SCRIPT_DIR/pyproject.toml" << 'EOF'
# pyproject.toml — mutmut phase 1 (nibe_mqtt_publisher.py)
# Written by run-mutmut.sh — do not edit manually during a run.
# source_paths uses a root-level copy so mutmut keys the module as
# 'nibe_mqtt_publisher.*' matching what tests record via __module__.
# pythonpath override makes mutants/nibe_mqtt_publisher.py importable as
# 'nibe_mqtt_publisher' and mutants/app/* importable as peer modules.

[tool.mutmut]
source_paths = ["nibe_mqtt_publisher.py"]
also_copy = [
    "tests",
    "pytest.ini",
    "app",
]
pytest_add_cli_args = [
    "--timeout=600",
    "--override-ini=pythonpath=. app",
    "-p", "no:randomly",
]
pytest_add_cli_args_test_selection = ["tests/test_mqtt_publisher.py"]
mutate_only_covered_lines = false
timeout_multiplier = 5.0
timeout_constant = 30.0
EOF
}

write_phase_2_config() {
cat > "$SCRIPT_DIR/pyproject.toml" << 'EOF'
# pyproject.toml — mutmut phase 2 (pure/near-pure modules)
# Written by run-mutmut.sh — do not edit manually during a run.

[tool.mutmut]
source_paths = [
    "nibe_entity_detection.py",
    "nibe_dynamic_map.py",
    "nibe_api.py",
]
also_copy = [
    "tests",
    "pytest.ini",
    "app",
]
pytest_add_cli_args = [
    "--timeout=600",
    "--override-ini=pythonpath=. app",
    "-p", "no:randomly",
]
pytest_add_cli_args_test_selection = [
    "tests/test_entity_detection.py",
    "tests/test_dynamic_map.py",
    "tests/test_api.py",
]
mutate_only_covered_lines = false
timeout_multiplier = 5.0
timeout_constant = 30.0
EOF
}

write_phase_3_config() {
cat > "$SCRIPT_DIR/pyproject.toml" << 'EOF'
# pyproject.toml — mutmut phase 3 (nibe_entity_manager.py full file)
# Written by run-mutmut.sh — do not edit manually during a run.
# Intended for overnight/weekend runs on ODROID-M1.
#
# NOTE: mutmut 3.x only_mutate uses fnmatch against file paths — function-level
# scoping is not supported. The full file is mutated (~3000-5000 mutants expected).

[tool.mutmut]
source_paths = ["nibe_entity_manager.py"]
also_copy = [
    "tests",
    "pytest.ini",
    "app",
]
pytest_add_cli_args = [
    "--timeout=600",
    "--override-ini=pythonpath=. app",
    "-p", "no:randomly",
]
pytest_add_cli_args_test_selection = [
    "tests/test_entity_manager.py",
    "tests/test_ha_integration.py",
    "tests/test_generate.py",
    "tests/test_lovelace.py",
]
mutate_only_covered_lines = false
timeout_multiplier = 5.0
timeout_constant = 30.0
EOF
}

echo "[mutmut] Phase $PHASE mutation run starting..."

case "$PHASE" in
1)
    echo "[mutmut] Target: app/nibe_mqtt_publisher.py"
    echo "[mutmut] Tests:  tests/test_mqtt_publisher.py (smart selection)"
    echo "[mutmut] Estimated runtime: ~1-2 hr on ODROID-M1"
    stage_module "nibe_mqtt_publisher"
    write_phase_1_config
    ;;
2)
    echo "[mutmut] Target: app/nibe_entity_detection.py, nibe_dynamic_map.py, nibe_api.py"
    echo "[mutmut] Tests:  matching test files only (smart selection)"
    echo "[mutmut] Estimated runtime: ~1-2 hr on ODROID-M1"
    stage_module "nibe_entity_detection"
    stage_module "nibe_dynamic_map"
    stage_module "nibe_api"
    write_phase_2_config
    ;;
3)
    echo "[mutmut] Target: app/nibe_entity_manager.py (full file — ~3000-5000 mutants)"
    echo "[mutmut] Tests:  test_entity_manager + test_ha_integration + test_generate + test_lovelace"
    echo "[mutmut] Estimated runtime: several hours — intended for overnight run"
    stage_module "nibe_entity_manager"
    write_phase_3_config
    ;;
*)
    echo "Unknown phase: $PHASE (valid: 1, 2, 3)" >&2
    exit 1
    ;;
esac

# Delete the mutants/ sandbox so mutmut rebuilds from the new config.
if [ -d "$SCRIPT_DIR/mutants" ]; then
    echo "[mutmut] Removing stale mutants/ sandbox..."
    rm -rf "$SCRIPT_DIR/mutants"
fi

cd "$SCRIPT_DIR"

mutmut run

echo ""
echo "[mutmut] Run complete. To inspect results:"
echo "  mutmut results"
echo "  mutmut show <mutant_id>"
