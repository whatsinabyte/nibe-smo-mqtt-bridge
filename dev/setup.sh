#!/bin/bash
# ==============================================================================
# One-time dev environment setup
# ==============================================================================
#
# Everything a fresh checkout of this repo needs beyond `git clone`, in one
# place. Written after a session where each of these was discovered by hand,
# one at a time, mid-session: a missing .venv-check, missing app/node_modules,
# no Docker/Colima/gh CLI at all on an Intel Mac (which can't use Homebrew —
# MacPorts is the package manager here), and a default Colima profile too
# small to run the dev/e2e/ harness without looking like the application was
# broken. Safe to re-run — every step below only acts if its target is
# missing, so running this again after some of it is already done is a no-op
# for those parts.
#
#   ./dev/setup.sh          run everything
#   ./dev/setup.sh --check  report what's missing without installing anything
#   ./dev/setup.sh --doctor actually run lint/type-check/both test suites —
#                           a health check, not just a presence check
#
# What this does NOT do: install Homebrew (this repo assumes MacPorts on
# Intel Macs — see the sudo-gated steps below, which this script deliberately
# does not attempt to run for you), or authenticate `gh` (interactive, needs
# your own browser/credentials).

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_ONLY=false
DOCTOR=false
case "${1:-}" in
    --check)  CHECK_ONLY=true ;;
    --doctor) DOCTOR=true ;;
esac

ok()   { echo "  [ok]   $1"; }
todo() { echo "  [todo] $1"; }
info() { echo "==> $1"; }
fail() { echo "  [FAIL] $1"; DOCTOR_FAILED=true; }
DOCTOR_FAILED=false

cd "$REPO"

if $DOCTOR; then
    # All checks below are independent and read-only (nothing here writes
    # code or config), so they run as background jobs instead of one after
    # another — this machine has 6 cores, and a fully serial doctor run
    # was leaving most of them idle for all but the pytest/vitest steps.
    # pytest and vitest each parallelize internally across all cores on
    # their own (pytest via -n auto, vitest via its own worker pool), so
    # running everything else concurrently with them does oversubscribe
    # the machine somewhat — but total wall-clock time is bounded by the
    # slowest single job either way, not the sum of all of them, so this
    # is still a real win over strictly sequential execution.
    info "Doctor: running lint, type-check, and both test suites for real (in parallel)"
    echo

    declare -a LABELS=()
    declare -a PIDS=()
    declare -a LOGS=()

    start_check() {
        # $1 = label, rest = command. Output is captured to a temp file
        # rather than printed live, since interleaved output from several
        # concurrent jobs would be unreadable — each job's full output is
        # printed together, in launch order, once everything has finished.
        local label="$1"
        shift
        local log
        log="$(mktemp)"
        ("$@" >"$log" 2>&1) &
        LABELS+=("$label")
        PIDS+=("$!")
        LOGS+=("$log")
    }

    start_check "ruff check"  .venv-check/bin/python -m ruff check app/ tests/
    start_check "ruff format" .venv-check/bin/python -m ruff format --check app/ tests/
    start_check "mypy"        .venv-check/bin/python -m mypy app/
    start_check "pip check"   .venv-check/bin/pip check
    # vulture/bandit/pip-audit are declared in requirements-dev.txt and
    # documented in CONTRIBUTING.md as manual commands, but weren't wired
    # into any automated check anywhere (not CI, not this script) — easy
    # to install them and then forget they exist. Included here so a
    # doctor run is the one place that actually exercises every static-
    # analysis tool this project has already paid the dependency cost for.
    start_check "vulture"     .venv-check/bin/python -m vulture --exclude node_modules app/ vulture_whitelist.py
    start_check "bandit"      .venv-check/bin/python -m bandit -r app/
    start_check "pip-audit"   .venv-check/bin/python -m pip_audit
    start_check "eslint"      bash -c "cd app && npx eslint ."
    # Not installed via pip/npm — added after this script itself shipped
    # two real bash bugs in one session (a negative array index needing
    # bash 4.3+ on a machine that ships bash 3.2, and an `A && B || C`
    # pseudo-if-then-else that doesn't do what it looks like), both of the
    # kind this linter flags by default. Only checks this repo's own
    # scripts, not vendored/node_modules content.
    if command -v shellcheck >/dev/null 2>&1; then
        start_check "shellcheck"  shellcheck run.sh run-mutmut.sh dev/setup.sh dev/mosquitto.sh dev/e2e/run.sh
    else
        echo "--- shellcheck ---"
        echo "shellcheck not installed — skipping (MacPorts: sudo port install shellcheck)"
        echo
    fi
    # pytest (-n auto, up to 6 xdist workers) and vitest (its own worker
    # pool) are each already internally parallel across all cores on their
    # own — launching both of them into the same free-for-all as everything
    # above caused a real, reproduced flake (vitest failed under
    # contention, then passed cleanly standalone every time after). So
    # these two run one after another instead, after everything lighter has
    # already been launched — they're the long poles anyway, so this costs
    # little wall-clock time while removing the double-worker-pool collision.
    start_check "pytest"      .venv-check/bin/python -m pytest tests/ -q -n auto --dist=loadscope
    # Negative array indices (${PIDS[-1]}) need bash 4.3+ — macOS ships
    # bash 3.2 by default, which this repo's shell scripts target (see
    # dev/mosquitto.sh/run-mutmut.sh), so index by count instead.
    pytest_index=$(( ${#PIDS[@]} - 1 ))
    wait "${PIDS[$pytest_index]}" >/dev/null 2>&1 || true  # let pytest finish alone
    start_check "vitest"      bash -c "cd app && npm test"

    for i in "${!PIDS[@]}"; do
        label="${LABELS[$i]}"
        log="${LOGS[$i]}"
        echo "--- $label ---"
        if wait "${PIDS[$i]}"; then
            cat "$log"
            ok "$label"
        else
            cat "$log"
            fail "$label"
        fi
        rm -f "$log"
        echo
    done

    if $DOCTOR_FAILED; then
        echo "==> Doctor found problems — see [FAIL] lines above."
        exit 1
    else
        echo "==> Doctor: everything healthy."
        exit 0
    fi
fi

# ---------------------------------------------------------------------------
# 1. Python venv (.venv, with .venv-check as the name run-mutmut.sh and the
#    integration tests' own docstrings prefer — see run-mutmut.sh's own
#    venv-search order for why both names need to resolve to the same venv).
# ---------------------------------------------------------------------------
info "Python virtual environment"

# mypy.ini pins python_version = 3.12 — a venv built from a different
# python3 would still "work" (create successfully) but silently diverge
# from what CI and every documented command assume. Fail loudly here
# instead of leaving that to surface later as a confusing mypy/test
# mismatch on whichever machine happens to have a different default python3.
PY_VERSION="$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
if [ "$PY_VERSION" != "3.12" ]; then
    if [ -d ".venv" ]; then
        ok "python3 is $PY_VERSION, but .venv already exists (not re-checking an existing venv's own version)"
    else
        echo "  [FAIL] python3 resolves to $PY_VERSION, but this project targets 3.12" >&2
        echo "         (see mypy.ini's python_version and CLAUDE.md's 'Python 3.12 target')." >&2
        echo "         Install/select a 3.12 python3 before creating .venv." >&2
        exit 1
    fi
fi

if [ ! -d ".venv" ]; then
    if $CHECK_ONLY; then
        todo ".venv does not exist (would run: python3 -m venv .venv)"
    else
        python3 -m venv .venv
        ok "Created .venv"
    fi
else
    ok ".venv exists"
fi

if [ ! -e ".venv-check" ]; then
    if $CHECK_ONLY; then
        todo ".venv-check does not exist (would symlink to .venv)"
    else
        ln -s .venv .venv-check
        ok "Symlinked .venv-check -> .venv"
    fi
else
    ok ".venv-check exists"
fi

if ! $CHECK_ONLY && [ -d ".venv" ]; then
    info "Installing/updating Python dependencies"
    .venv/bin/pip install -q -U -r app/requirements.txt -r requirements-test.txt -r requirements-dev.txt
    ok "Python dependencies up to date"
fi

info "pre-commit hook"
if [ ! -f ".git/hooks/pre-commit" ] || ! grep -q "pre-commit.com" ".git/hooks/pre-commit" 2>/dev/null; then
    if $CHECK_ONLY; then
        todo "pre-commit hook not installed (would run: pre-commit install)"
    else
        .venv-check/bin/pre-commit install
        ok "pre-commit hook installed"
    fi
else
    ok "pre-commit hook already installed"
fi

# ---------------------------------------------------------------------------
# 2. JS dependencies — the card's own test suite (app/) and the e2e harness
#    (dev/e2e/) each have their own package.json, independent of each other.
# ---------------------------------------------------------------------------
info "JavaScript dependencies"
for dir in app dev/e2e; do
    if [ ! -d "$dir/node_modules" ]; then
        if $CHECK_ONLY; then
            todo "$dir/node_modules missing (would run: npm install in $dir)"
        else
            (cd "$dir" && npm install --silent)
            ok "Installed $dir/node_modules"
        fi
    else
        ok "$dir/node_modules exists"
    fi
done

# ---------------------------------------------------------------------------
# 3. Docker / Colima — needed for dev/mosquitto.sh and dev/e2e/. This repo
#    is developed on an Intel Mac, where Homebrew is not an option (dropped
#    Intel support) — MacPorts is the package manager. This script checks
#    and reports; it does not run `sudo port install` for you.
# ---------------------------------------------------------------------------
info "Docker / Colima"
if ! command -v docker >/dev/null 2>&1; then
    todo "docker not found — install via MacPorts: sudo port install docker colima docker-compose"
elif ! command -v colima >/dev/null 2>&1; then
    ok "docker found"
    todo "colima not found — install via MacPorts: sudo port install colima"
else
    ok "docker and colima found"
    if colima status >/dev/null 2>&1; then
        alloc="$(colima list 2>/dev/null | awk '$1=="default"{print $4" CPU / "$5" RAM"}')"
        if [ -n "$alloc" ]; then
            ok "colima running (${alloc})"
            case "$alloc" in
                "4 CPU"*|[5-9]*|[1-9][0-9]*) : ;;  # 4+ CPU, good enough
                *) todo "colima is under-resourced for dev/e2e/ — see dev/e2e/README.md's Colima resource sizing note (needs 4 CPU / 4GiB minimum: colima stop && colima start --cpu 4 --memory 4)" ;;
            esac
        fi
    else
        todo "colima installed but not running — start it: colima start --cpu 4 --memory 4"
    fi
    # Compose v2 plugin — MacPorts' docker port only ships the v1 standalone
    # binary; see dev/e2e/README.md's Compose v1/v2 note for the fix.
    if docker compose version >/dev/null 2>&1; then
        ok "docker compose (v2 plugin) works"
    else
        todo "docker compose (v2) not working — see dev/e2e/README.md's Compose v1/v2 note"
    fi
fi

# ---------------------------------------------------------------------------
# 4. Playwright browser (for dev/e2e/'s Chromium-driven test).
# ---------------------------------------------------------------------------
info "Playwright browser"
if [ -d "dev/e2e/node_modules" ]; then
    if (cd dev/e2e && npx playwright --version >/dev/null 2>&1); then
        if $CHECK_ONLY; then
            todo "Playwright installed — run 'npx playwright install --with-deps chromium' in dev/e2e/ if Chromium itself isn't installed yet (this script doesn't check that)"
        else
            (cd dev/e2e && npx playwright install --with-deps chromium)
            ok "Playwright Chromium installed"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 5. gh CLI — for posting to GitHub issues/PRs directly.
# ---------------------------------------------------------------------------
info "GitHub CLI"
if ! command -v gh >/dev/null 2>&1; then
    todo "gh not found — install via MacPorts: sudo port install gh, then: gh auth login"
elif ! gh auth status >/dev/null 2>&1; then
    ok "gh found"
    todo "gh not authenticated — run: gh auth login"
else
    ok "gh found and authenticated"
fi

echo
info "Done. Re-run with --check to see status without installing anything."
