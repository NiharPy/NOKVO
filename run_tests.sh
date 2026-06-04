#!/bin/bash
# Default test run — unit tests + FSM eval framework against the FakeDB.
# To also run the integration suite against a real Postgres (catches the
# greenlet_spawn / session-corruption class of bugs the FakeDB hides),
# export TEST_DATABASE_URL to an async DSN before invoking this script:
#
#   export TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/nokvo_test
#   ./run_tests.sh
#
# Without TEST_DATABASE_URL, tests/integration/ tests skip cleanly.
source venv/bin/activate
PYTHONPATH=. pytest -v tests/
