# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Home Assistant custom component (`ezviz_stream`) that integrates into Ezviz's developer API's to stream from their cloud services. 

## Common Commands

### Development
```bash
.devcontainer/scripts/setup    # Install dependencies and pre-commit hooks
.devcontainer/scripts/develop  # Start a local Home Assistant instance (port 8123)
.devcontainer/scripts/clean    # Reset Home Assistant config directory
```

### Linting & Formatting
```bash
.devcontainer/scripts/lint     # Run ruff format + ruff check --fix
ruff check .                   # Lint only (CI uses this)
ruff format . --check          # Format check only (CI uses this)
pre-commit run --all-files     # Run all pre-commit hooks (pyupgrade, black, codespell, ruff)
```

### Testing
```bash
pytest tests/                  # Run all tests
pytest tests/test_foo.py       # Run a single test file
pytest tests/test_foo.py::test_bar -v  # Run a single test
```

Tests use `pytest-homeassistant-custom-component` which provides Home Assistant's test infrastructure. See `tests/conftest.py` for fixtures (`mock_qsclient`, `setup_integration`).

## Architecture

### Data Flow
```
User action → Light/Switch.turn_on/off()
  → BaseEntity.control_device_optimistic() → CommandQueue.enqueue_set_device()
  → CommandQueue processes with priority & delay → QSClient API call
  → UI shows optimistic value immediately

Coordinator polls periodically (default 5s)
  → CommandQueue.enqueue_poll() → QSClient.get_devices_status()
  → Coordinator distributes data → Entities reconcile optimistic values
```

### Key Modules (in `custom_components/ezviz_stream/`)


### External Dependency


## Ruff Configuration

All lint rules enabled (`select = ["ALL"]`) with specific exclusions. Target: Python 3.13. Max complexity: 25. Test files have relaxed rules (asserts, magic values, missing docstrings allowed). See `.ruff.toml` for details.

## CI/CD

Two GitHub Actions workflows on push/PR to main:
- **lint.yml** — `ruff check` and `ruff format --check`
- **validate.yml** — Home Assistant `hassfest` manifest validation and HACS validation

## Git Workflow

- **Always commit all changed and new files.** Never create partial commits — every commit should include the complete set of changes.

## Config Entry


