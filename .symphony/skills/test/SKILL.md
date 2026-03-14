---
name: test
description: Run and validate the test suite; use when asked to test, after code changes, or during validation.
---

# Test

## Goals

- Verify all tests pass before and after changes.
- Ensure test count does not decrease.
- Properly mock external dependencies.

## Commands

```bash
# Full test suite (primary gate)
cd backend && .venv/bin/python -m pytest tests/ -x -q

# Run specific test file
cd backend && .venv/bin/python -m pytest tests/test_specific.py -x -v

# Run with coverage
cd backend && .venv/bin/python -m pytest tests/ --cov=. --cov-report=term-missing -x -q

# Lint check
cd backend && .venv/bin/ruff check .
```

## Test Conventions

- All tests use in-memory SQLite via aiosqlite (not PostgreSQL).
- All external API calls (exchange, LLM, Discord) must be mocked.
- Use `pytest-asyncio` for async tests.
- Test files mirror source structure: `backend/engine/` → `tests/test_engine/`.
- Fixture pattern: `conftest.py` per test directory.

## Rules

- Current test count: 789+. Never decrease.
- Run the FULL suite, not just new tests.
- If tests fail, fix the code (not the tests) unless the test itself is wrong.
- Record test results in the workpad Validation section.

## Writing New Tests

1. Mirror the source file location in the test directory.
2. Test both happy path and edge cases.
3. Mock external dependencies at the boundary (adapter/client level).
4. Use descriptive test names: `test_<scenario>_<expected_result>`.
5. For strategy changes: include signal generation tests with known inputs/outputs.
