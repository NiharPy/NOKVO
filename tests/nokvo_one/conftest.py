"""Override the parent conftest's DB-bound autouse fixture for Nokvo One unit tests.

The unit tests in this folder do not need a live Postgres connection. Returning a
no-op autouse fixture short-circuits the parent setup_teardown_db so the unit tests
run on a developer machine without DB access.
"""
import pytest


@pytest.fixture(scope="session", autouse=True)
def setup_teardown_db():
    yield
