"""Fixtures shared by the issue #342 durable-Batch interrupt suites.

Only what pytest must resolve BY NAME lives here — a fixture cannot be
imported without tripping the shadowing rule that makes every test using
it look like a redefinition. Callable helpers stay in
``_batch_interrupt.py`` and are imported explicitly, so each suite's file
header still says exactly what it uses.

Every pre-existing ticket suite defines its own module-level ``home``,
which takes precedence over this one; nothing there changes.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from hypergraph import RunHome
from tests.test_host._ingestion_fixture import LEDGER_ENV


@pytest_asyncio.fixture
async def home(tmp_path):
    """A FRESH world: an empty SQLite Run Home, never hand-seeded."""
    h = RunHome.open(f"file:{tmp_path / 'runs.db'}")
    yield h
    await h.close()


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """The deterministic domain-effect ledger for this test."""
    path = tmp_path / "effects.log"
    monkeypatch.setenv(LEDGER_ENV, str(path))
    return path
