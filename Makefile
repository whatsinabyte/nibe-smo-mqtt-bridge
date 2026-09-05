# Thin convenience layer over commands documented in full in CLAUDE.md /
# CONTRIBUTING.md — this does not replace reading those, it just saves
# retyping the fully-qualified .venv-check paths and directory changes
# those commands need. Every target here is a one-line wrapper around a
# command that also works fine typed out by hand.

.PHONY: setup check doctor test test-js test-e2e lint format typecheck mutmut broker-up broker-down clean

setup:      ## One-time/ongoing dev environment setup (see dev/setup.sh's own header)
	./dev/setup.sh

check:      ## Report what's missing from the dev environment without installing anything
	./dev/setup.sh --check

doctor:     ## Actually run lint + type-check + both test suites (parallelized across cores)
	./dev/setup.sh --doctor

test:       ## Python test suite (parallel, matches CI)
	.venv-check/bin/python -m pytest tests/ -q -n auto --dist=loadscope

test-js:    ## Card's own Vitest suite
	cd app && npm test

test-e2e:   ## Manual, on-demand real-stack e2e harness (see dev/e2e/README.md)
	cd dev/e2e && ./run.sh

lint:       ## ruff check + eslint
	.venv-check/bin/python -m ruff check app/ tests/
	cd app && npx eslint .

format:     ## ruff format (writes changes)
	.venv-check/bin/python -m ruff format app/ tests/

typecheck:  ## mypy
	.venv-check/bin/python -m mypy app/

mutmut:     ## Mutation testing — see run-mutmut.sh --list for module names; slow, use sparingly
	./run-mutmut.sh --list

broker-up:  ## Start the disposable dev Mosquitto broker (dev/mosquitto.sh)
	./dev/mosquitto.sh start

broker-down: ## Stop it
	./dev/mosquitto.sh stop

clean:      ## Remove caches/build artifacts this Makefile's own targets can leave behind
	rm -rf .ruff_cache .mypy_cache .pytest_cache
