## What this PR does

A clear description of the change and why it is needed. Reference any related issue with `Fixes #123` or `Closes #123`.

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Documentation update
- [ ] Refactor (no behaviour change)
- [ ] Other — describe:

## Checklist

- [ ] Tests pass — `pytest tests/ --cov=app --cov-report=term-missing`
- [ ] Ruff clean — `ruff check app/ tests/`
- [ ] Mypy clean — `mypy app/`
- [ ] Vulture clean — `vulture app/`
- [ ] Bandit clean — `bandit -r app/`
- [ ] `CHANGELOG.md` updated under `[Unreleased]`
- [ ] New behaviour is covered by tests
- [ ] Module boundaries respected — see [ARCHITECTURE.md](https://github.com/whatsinabyte/nibe-smo-mqtt-bridge/blob/main/ARCHITECTURE.md)

## Testing notes

How was this tested? Include the controller model and firmware version if the change involves firmware interaction.

## Screenshots or log output

If relevant — before/after log lines, HA entity screenshots, dashboard changes.
