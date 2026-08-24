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
# This script copies the target module to the repo root before running
# mutmut. The copy is kept after the run so that mutmut show/results work
# (mutmut show needs the source file to apply patches against). Always edit
# app/<module>.py — the root-level copy is regenerated on every run, and is
# deleted at the end of a full ("all") run so it can't shadow app/ during a
# later plain `pytest` invocation (pytest.ini sets pythonpath = ". app",
# so a stale root-level copy silently wins over app/'s real file).
# The sandbox overrides pythonpath to '. app' so both the mutated file
# (at mutants/) and peer modules (at mutants/app/) are importable.
#
# ONE MODULE AT A TIME, NOT PHASES
# This used to bundle several unrelated source modules into one shared
# mutants/ sandbox per "phase" (1-4). That was replaced (2026-08-22) after
# a phase-4 run crashed mid-way (see MUTMUT RELIABILITY CAVEAT below) and
# corrupted survivor verdicts for every module sharing that run, not just
# the one that mattered — there was no way to trust or re-inspect one
# module's results without the others' noise. Processing one module per
# mutmut invocation means a crash or bad result only ever affects that one
# module, each module gets its own saved results file to review
# independently, and worker concurrency does not need throttling per
# module (each run's own test suite is much smaller than the full 4000+
# test project suite, so there is no per-run OOM risk to guard against —
# see MEMORY-CONSTRAINED MACHINES below for when to still cap it anyway).
#
# USAGE
#   ./run-mutmut.sh              — run every module below, sequentially
#   ./run-mutmut.sh <module>     — run just one module (e.g. nibe_lovelace)
#   ./run-mutmut.sh --list       — print the module list and exit
#
# Each module's mutmut run produces:
#   mutmut-results/<module>.txt        — full `mutmut results` output
#   mutmut-results/SUMMARY.txt         — one line per module (survived/total),
#                                         appended to across the whole run
#   mutants-<module>/                  — that module's own full mutmut
#                                         sandbox, archived (not shared/
#                                         overwritten) so `mutmut show` stays
#                                         available for EVERY module later,
#                                         not just whichever ran last
# so a full ("all") run leaves one results file AND one archived sandbox per
# module to pick through, plus a scannable summary index, instead of one
# shared mutants/ that only ever reflects the most recent module.
#
# INSPECTING AN ARCHIVED MODULE'S MUTANTS LATER
#   mutmut hardcodes the literal directory name "mutants" relative to cwd
#   throughout its own source — there is no config option to rename or
#   relocate it, so `mutmut show`/`mutmut results` only ever read from
#   ./mutants. To inspect a module that isn't the one most recently run:
#     rm -rf mutants && cp -r mutants-<module> mutants && mutmut show <id>
#   (cp, not mv, so the archive under mutants-<module>/ survives for next
#   time too.)
#
# RESUMING AN INTERRUPTED RUN
# A module's mutants/ sandbox is only renamed to mutants-<module>/ after
# that module's run finishes — an interrupted run leaves a half-finished
# plain mutants/ behind, which the next invocation of this script discards
# as stale before starting fresh (mutmut's own `mutmut run` resume-from-
# interruption behavior only works within a single still-in-progress
# mutants/, which this script does not attempt to preserve across restarts).
# To resume just the interrupted module, run it by name again — it starts
# over from scratch, not from where it left off.
#
# INTERPRETING SURVIVORS
#   Add a test that pins the exact value/condition, or annotate the line
#   with a comment documenting genuinely equivalent mutations (e.g. log
#   strings whose case doesn't affect a re.IGNORECASE regex match).
#
# ⚠ MUTMUT RELIABILITY CAVEAT — READ BEFORE TRUSTING ANY OUTPUT HERE ⚠
#   mutmut 3.7.0 (pinned in this project) calls pytest.main() IN-PROCESS —
#   once for its own baseline "which tests cover which mutants" stats
#   collection, then again per mutant tested, all in the same worker
#   process. Calling pytest.main() more than once in the same interpreter
#   is explicitly unsupported by pytest and has been observed on this
#   project to corrupt pytest's own tmp_path_factory cleanup, crashing that
#   worker and producing false "no tests"/"survived" verdicts — including,
#   on one occasion, a module reporting fresh survivors in functions whose
#   every survivor had just been individually closed and empirically
#   verified moments earlier. Splitting phases into one-module-per-run (this
#   script) reduces the blast radius and eliminated the "no tests" symptom
#   in testing, but did NOT eliminate false "survived" verdicts even in a
#   single-module isolated run — a genuine missed gap and confirmed false
#   positives were found sitting side by side in the same run's survivor
#   list. There is no known settings fix for this (concurrency capping does
#   not help — it is not a concurrency race). CONCLUSION: treat every
#   "survived" verdict in mutmut-results/*.txt as an unverified hypothesis,
#   not a fact. Before writing a test for one, independently apply the exact
#   diff from `mutmut show <id>` by hand to app/<module>.py and confirm with
#   a PLAIN `pytest tests/test_<x>.py -q` run (not through mutmut) that the
#   real suite does not already catch it — and after fixing a batch, spot-
#   check a couple of "killed" mutants the same way too, since a corrupted
#   run's false negatives are just as unverified as its false positives.
#   `mutmut show <id>` itself remains reliable (it only applies a patch and
#   prints the diff — no test execution involved); it is specifically the
#   coverage-collection/survived-verdict machinery that is compromised.
#
# MEMORY-CONSTRAINED MACHINES
#   mutmut defaults to one worker per CPU core. nibe_entity_manager.py's
#   test suite (12 test files, thousands of tests) is large enough that this
#   caused an OOM-kill on a 6-core/8GB Mac in earlier testing. Set
#   MUTMUT_MAX_CHILDREN to cap worker concurrency if this recurs, e.g.:
#     MUTMUT_MAX_CHILDREN=3 ./run-mutmut.sh nibe_entity_manager
#   Smaller modules' own test suites are small enough that this has not
#   been necessary — the default (all cores) is fine for those.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESULTS_DIR="$SCRIPT_DIR/mutmut-results"
ARG="${1:-}"

# MODULE LIST
# module_name|test_file1,test_file2,...
# Test-file sets are carried forward unchanged from the former phase
# configs (never narrowed to a single guessed-relevant file) so no module
# silently loses coverage from tests in another file that happen to
# exercise it — e.g. nibe_discovery_config.py's original phase 1 config
# included test_lovelace.py alongside its own test file, so that stays.
MODULES=(
    "nibe_mqtt_publisher|tests/test_mqtt_publisher.py,tests/test_lovelace.py,tests/test_discovery_config.py"
    "nibe_discovery_config|tests/test_mqtt_publisher.py,tests/test_lovelace.py,tests/test_discovery_config.py"
    "nibe_entity_detection|tests/test_entity_detection.py,tests/test_dynamic_map.py,tests/test_api.py"
    "nibe_dynamic_map|tests/test_entity_detection.py,tests/test_dynamic_map.py,tests/test_api.py"
    "nibe_api|tests/test_entity_detection.py,tests/test_dynamic_map.py,tests/test_api.py"
    "nibe_entity_manager|tests/test_entity_manager.py,tests/test_entity_manager_changelog.py,tests/test_entity_manager_commands.py,tests/test_entity_manager_discovery.py,tests/test_entity_manager_dynamic.py,tests/test_entity_manager_lifecycle.py,tests/test_entity_manager_polling.py,tests/test_entity_manager_snapshots.py,tests/test_entity_manager_state.py,tests/test_ha_integration.py,tests/test_generate.py,tests/test_lovelace.py"
    "nibe_ha_integration|tests/test_ha_integration.py,tests/test_lovelace.py,tests/test_caching.py,tests/test_utils.py,tests/test_generate.py,tests/test_connectivity_check.py"
    "nibe_lovelace|tests/test_ha_integration.py,tests/test_lovelace.py,tests/test_caching.py,tests/test_utils.py,tests/test_generate.py,tests/test_connectivity_check.py"
    "nibe_caching|tests/test_ha_integration.py,tests/test_lovelace.py,tests/test_caching.py,tests/test_utils.py,tests/test_generate.py,tests/test_connectivity_check.py"
    "nibe_test_runner|tests/test_ha_integration.py,tests/test_lovelace.py,tests/test_caching.py,tests/test_utils.py,tests/test_generate.py,tests/test_connectivity_check.py"
    "nibe_utils|tests/test_ha_integration.py,tests/test_lovelace.py,tests/test_caching.py,tests/test_utils.py,tests/test_generate.py,tests/test_connectivity_check.py"
    "generate_nibe_mqtt|tests/test_ha_integration.py,tests/test_lovelace.py,tests/test_caching.py,tests/test_utils.py,tests/test_generate.py,tests/test_connectivity_check.py"
    "nibe_connectivity_check|tests/test_ha_integration.py,tests/test_lovelace.py,tests/test_caching.py,tests/test_utils.py,tests/test_generate.py,tests/test_connectivity_check.py"
)

if [ "$ARG" = "--list" ]; then
    for entry in "${MODULES[@]}"; do
        echo "${entry%%|*}"
    done
    exit 0
fi

# VENV DISCOVERY
for _venv_dir in ".venv-check" ".venv" "venv"; do
    if [ -x "$SCRIPT_DIR/$_venv_dir/bin/mutmut" ]; then
        PATH="$SCRIPT_DIR/$_venv_dir/bin:$PATH"
        echo "[mutmut] Using venv: $_venv_dir/"
        break
    fi
done
unset _venv_dir

if ! command -v mutmut >/dev/null 2>&1; then
    echo "[mutmut] ERROR: 'mutmut' not found on PATH and no venv with it was" >&2
    echo "  found at $SCRIPT_DIR/{.venv-check,.venv,venv}/bin/mutmut." >&2
    echo "  Activate the project venv first, or install mutmut into one of" >&2
    echo "  those locations." >&2
    exit 1
fi

mkdir -p "$RESULTS_DIR"

run_one_module() {
    local module="$1"
    local test_files_csv="$2"
    IFS=',' read -ra test_files <<< "$test_files_csv"

    echo ""
    echo "=================================================================="
    echo "[mutmut] Module: ${module}.py"
    echo "=================================================================="

    cp "$SCRIPT_DIR/app/${module}.py" "$SCRIPT_DIR/${module}.py"
    echo "[mutmut] Staged: ${module}.py (copy of app/${module}.py — kept for mutmut show)"

    if [ "$module" = "nibe_lovelace" ]; then
        cp "$SCRIPT_DIR/app/menu_structure.yaml" "$SCRIPT_DIR/menu_structure.yaml"
        echo "[mutmut] Staged: menu_structure.yaml (nibe_lovelace __file__-relative lookup)"
    fi

    {
        echo "[tool.mutmut]"
        echo "source_paths = [\"${module}.py\"]"
        echo "also_copy = ["
        echo "    \"tests\","
        echo "    \"pytest.ini\","
        echo "    \"app\","
        echo "    \"config.yaml\","
        echo "    \"menu_structure.yaml\","
        echo "    \"translations\","
        echo "]"
        echo "pytest_add_cli_args = ["
        echo "    \"--timeout=600\","
        echo "    \"--override-ini=pythonpath=. app\","
        echo "    \"-p\", \"no:randomly\","
        echo "]"
        echo "pytest_add_cli_args_test_selection = ["
        for tf in "${test_files[@]}"; do
            echo "    \"${tf}\","
        done
        echo "]"
        echo "mutate_only_covered_lines = false"
        echo "timeout_multiplier = 5.0"
        echo "timeout_constant = 30.0"
    } > "$SCRIPT_DIR/pyproject.toml"

    # Any plain mutants/ left over here can only be a half-finished sandbox
    # from an interrupted earlier run (a completed run always renames it
    # away to mutants-<module>/ below before this point is reached again) —
    # safe to discard.
    echo "[mutmut] Removing any stale mutants/ sandbox (interrupted-run leftover)..."
    rm -rf "$SCRIPT_DIR/mutants"

    cd "$SCRIPT_DIR"
    if [ -n "${MUTMUT_MAX_CHILDREN:-}" ]; then
        echo "[mutmut] Capping worker concurrency: --max-children ${MUTMUT_MAX_CHILDREN}"
        mutmut run --max-children "$MUTMUT_MAX_CHILDREN" || true
    else
        mutmut run || true
    fi

    local results_file="$RESULTS_DIR/${module}.txt"
    mutmut results > "$results_file" 2>&1 || true
    local survived
    survived="$(grep -c ': survived' "$results_file" || true)"
    local no_tests
    no_tests="$(grep -c ': no tests' "$results_file" || true)"
    local total
    total="$(wc -l < "$results_file" | tr -d ' ')"
    echo "[mutmut] ${module}.py: ${survived} survived, ${no_tests} no-tests, ${total} lines total -> ${results_file}"
    echo "$(date '+%Y-%m-%d %H:%M:%S')  ${module}  survived=${survived}  no_tests=${no_tests}  results_lines=${total}" >> "$RESULTS_DIR/SUMMARY.txt"

    # Archive this module's own mutants/ sandbox under a per-module name
    # BEFORE the next module's run wipes plain mutants/ — otherwise `mutmut
    # show` only ever works for whichever module ran last, since mutmut
    # hardcodes the literal directory name "mutants" relative to cwd (no
    # config option to rename/relocate it — see project memory). Each
    # module keeps its own full sandbox this way, inspectable later via:
    #   rm -rf mutants && cp -r mutants-<module> mutants && mutmut show <id>
    if [ -d "$SCRIPT_DIR/mutants" ]; then
        rm -rf "$SCRIPT_DIR/mutants-${module}"
        mv "$SCRIPT_DIR/mutants" "$SCRIPT_DIR/mutants-${module}"
        echo "[mutmut] Archived sandbox: mutants-${module}/ (for later 'mutmut show' access)"
    fi
}

if [ -n "$ARG" ]; then
    found=""
    for entry in "${MODULES[@]}"; do
        name="${entry%%|*}"
        if [ "$name" = "$ARG" ]; then
            found="$entry"
            break
        fi
    done
    if [ -z "$found" ]; then
        echo "[mutmut] ERROR: unknown module '$ARG'. Run './run-mutmut.sh --list' to see valid names." >&2
        exit 1
    fi
    run_one_module "${found%%|*}" "${found#*|}"
else
    echo "[mutmut] No module given — running all ${#MODULES[@]} modules sequentially."
    for entry in "${MODULES[@]}"; do
        run_one_module "${entry%%|*}" "${entry#*|}"
    done
    # Clean up root-level staging copies after a full run so they can't
    # shadow app/ during a later plain `pytest` invocation.
    echo ""
    echo "[mutmut] Full run complete — removing root-level staging copies..."
    for entry in "${MODULES[@]}"; do
        rm -f "$SCRIPT_DIR/${entry%%|*}.py"
    done
    rm -f "$SCRIPT_DIR/menu_structure.yaml"
fi

echo ""
echo "[mutmut] Done. Results saved under: ${RESULTS_DIR}/"
echo "  cat ${RESULTS_DIR}/SUMMARY.txt                       — one line per module run this session"
echo "  cat ${RESULTS_DIR}/<module>.txt                      — full mutmut results for one module"
echo "  rm -rf mutants && cp -r mutants-<module> mutants      — restore a module's sandbox for 'mutmut show'"
echo "  mutmut show <mutant_id>                              — diff for a specific mutant (after restoring above)"
echo ""
echo "Remember the reliability caveat at the top of this script: verify"
echo "every 'survived' result by hand before trusting it."
