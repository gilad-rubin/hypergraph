"""Typed Diagnostic contract tests (#233, red-green items 4 and 5).

Item 4 — ``get_failure_evidence(...)`` returns a diagnostic with a stable
code + docs_ref for each terminal path in the locked precedence table.
Item 5 — snapshot test on the ``hypergraph.diagnostic/v1`` wire schema
(additive evolution guard).

Repo flaky rule: no wall-clock assertions. Deterministic terminal paths use
``jitter="none"`` and delays that decide the outcome structurally (e.g. an
``initial_delay`` beyond the retry window means terminal-without-sleeping).
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
import pytest_asyncio

from hypergraph import (
    AsyncRunner,
    AttemptOutcomeUnknownError,
    AttemptTimeoutError,
    Graph,
    IncompatibleRunnerError,
    RetryPolicy,
    RetryWindowExpiredError,
    SyncRunner,
    get_failure_evidence,
    node,
)
from hypergraph.checkpointers import AttemptStatus, SqliteCheckpointer
from hypergraph.diagnostics import (
    DIAGNOSTIC_CODES,
    Diagnostic,
    DiagnosticContext,
    DiagnosticFix,
    DiagnosticLocation,
)

aiosqlite = pytest.importorskip("aiosqlite")

REPO_ROOT = Path(__file__).resolve().parents[1]

ALL_CODES = {
    "HG_NODE_FAILED",
    "HG_RETRY_POLICY_INVALID",
    "HG_TIMEOUT_UNSUPPORTED",
    "HG_ATTEMPT_TIMEOUT",
    "HG_RETRY_EXHAUSTED",
    "HG_RETRY_WINDOW_EXPIRED",
    "HG_ATTEMPT_OUTCOME_UNKNOWN",
    "HG_RETRY_POLICY_CHANGED",
    "HG_ATTEMPT_PERSISTENCE_FAILED",
    "HG_RUNNER_POLICY_UNSUPPORTED",
}


def _policy(**overrides) -> RetryPolicy:
    kwargs = {
        "max_attempts": 3,
        "retry_on": (ConnectionError,),
        "initial_delay": 0.001,
        "jitter": "none",
    }
    kwargs.update(overrides)
    return RetryPolicy(**kwargs)


def _make_runner(family: str, **kwargs):
    return SyncRunner(**kwargs) if family == "sync" else AsyncRunner(**kwargs)


async def _run(runner, *args, **kwargs):
    result = runner.run(*args, **kwargs)
    if inspect.iscoroutine(result):
        result = await result
    return result


@pytest.fixture(params=["sync", "async"])
def family(request) -> str:
    return request.param


@pytest_asyncio.fixture
async def make_sqlite(tmp_path):
    created = []

    def factory(name: str = "diag.db") -> SqliteCheckpointer:
        cp = SqliteCheckpointer(str(tmp_path / name))
        created.append(cp)
        return cp

    yield factory
    for cp in created:
        await cp.close()


# === Item 5: wire-schema snapshot (additive evolution guard) ===


def test_diagnostic_wire_schema_snapshot():
    diagnostic = Diagnostic(
        code="HG_RETRY_EXHAUSTED",
        severity="error",
        problem="Node 'call_model' raised ConnectionError on the final permitted attempt.",
        location=DiagnosticLocation(
            node_name="call_model",
            graph_name="pipeline",
            superstep=2,
            item_index=None,
            workflow_id="wf-1",
        ),
        context=DiagnosticContext(
            error_type="ConnectionError",
            attempt_count=3,
            max_attempts=3,
            limit="max_attempts",
        ),
        how_to_fix=(DiagnosticFix("Fork or start a new workflow to grant a fresh retry budget."),),
        docs_ref="docs/06-api-reference/errors.md#hg-retry-exhausted",
    )
    wire = diagnostic.to_wire()

    # SNAPSHOT: codes and context field meanings are stable; evolution is
    # additive-only. Changing or removing any key below is a breaking change
    # to the hypergraph.diagnostic/v1 wire contract.
    assert wire == {
        "schema": "hypergraph.diagnostic/v1",
        "code": "HG_RETRY_EXHAUSTED",
        "severity": "error",
        "problem": "Node 'call_model' raised ConnectionError on the final permitted attempt.",
        "location": {
            "node_name": "call_model",
            "graph_name": "pipeline",
            "superstep": 2,
            "item_index": None,
            "workflow_id": "wf-1",
        },
        "context": {
            "error_type": "ConnectionError",
            "attempt_count": 3,
            "max_attempts": 3,
            "limit": "max_attempts",
            "timeout_seconds": None,
            "retry_window_seconds": None,
            "deadline_elapsed": None,
            "cancellation_requested": None,
        },
        "how_to_fix": ["Fork or start a new workflow to grant a fresh retry budget."],
        "docs_ref": "docs/06-api-reference/errors.md#hg-retry-exhausted",
    }


def test_registry_covers_all_ten_codes_with_docs_anchors():
    """Every locked code has a registry entry whose docs_ref anchor exists."""
    assert set(DIAGNOSTIC_CODES) == ALL_CODES
    errors_md = (REPO_ROOT / "docs" / "06-api-reference" / "errors.md").read_text()
    for code, docs_ref in DIAGNOSTIC_CODES.items():
        path, _, anchor = docs_ref.partition("#")
        assert path == "docs/06-api-reference/errors.md", code
        assert anchor, code
        assert f'id="{anchor}"' in errors_md, f"{code}: anchor #{anchor} missing from errors.md"


# === Item 4: stable code + docs_ref per terminal path (locked precedence table) ===


def _sole_evidence(error):
    evidence = get_failure_evidence(error)
    assert len(evidence) == 1
    return evidence[0]


async def test_plain_failure_maps_to_hg_node_failed(family):
    @node(output_name="value")
    def boom(x: int) -> int:
        raise ValueError("bad input")

    with pytest.raises(ValueError) as excinfo:
        await _run(_make_runner(family), Graph([boom]), {"x": 1})

    diagnostic = _sole_evidence(excinfo.value).diagnostic
    assert diagnostic.code == "HG_NODE_FAILED"
    assert diagnostic.docs_ref == "docs/06-api-reference/errors.md#hg-node-failed"
    assert diagnostic.severity == "error"
    assert diagnostic.location.node_name == "boom"
    assert diagnostic.context.error_type == "ValueError"


async def test_ineligible_failure_with_policy_maps_to_hg_node_failed(family):
    calls: list[int] = []

    @node(output_name="value", retry=_policy())
    def boom(x: int) -> int:
        calls.append(x)
        raise TypeError("permanent")

    with pytest.raises(TypeError) as excinfo:
        await _run(_make_runner(family), Graph([boom]), {"x": 1})

    assert calls == [1], "an ineligible failure never repeats"
    diagnostic = _sole_evidence(excinfo.value).diagnostic
    assert diagnostic.code == "HG_NODE_FAILED"
    assert diagnostic.context.error_type == "TypeError"


async def test_exhausted_budget_maps_to_hg_retry_exhausted(family):
    @node(output_name="value", retry=_policy(max_attempts=2))
    def doomed(x: int) -> int:
        raise ConnectionError("always")

    with pytest.raises(ConnectionError) as excinfo:
        await _run(_make_runner(family), Graph([doomed]), {"x": 1})

    diagnostic = _sole_evidence(excinfo.value).diagnostic
    assert diagnostic.code == "HG_RETRY_EXHAUSTED"
    assert diagnostic.docs_ref == "docs/06-api-reference/errors.md#hg-retry-exhausted"
    assert diagnostic.context.limit == "max_attempts"
    assert diagnostic.context.attempt_count == 2
    assert diagnostic.context.max_attempts == 2


async def test_window_blocked_retry_maps_to_hg_retry_exhausted_retry_window(family):
    """A wake time at/beyond the series deadline is terminal without sleeping."""

    @node(
        output_name="value",
        retry=_policy(max_attempts=5, retry_window=10.0, initial_delay=3600.0),
    )
    def doomed(x: int) -> int:
        raise ConnectionError("always")

    with pytest.raises(ConnectionError) as excinfo:
        await _run(_make_runner(family), Graph([doomed]), {"x": 1})

    diagnostic = _sole_evidence(excinfo.value).diagnostic
    assert diagnostic.code == "HG_RETRY_EXHAUSTED"
    assert diagnostic.context.limit == "retry_window"


async def test_attempt_timeout_maps_to_hg_attempt_timeout():
    import asyncio

    @node(output_name="value", timeout=0.05)
    async def hang(x: int) -> int:
        await asyncio.Event().wait()
        return x

    with pytest.raises(AttemptTimeoutError) as excinfo:
        await AsyncRunner().run(Graph([hang]), {"x": 1})

    diagnostic = _sole_evidence(excinfo.value).diagnostic
    assert diagnostic.code == "HG_ATTEMPT_TIMEOUT"
    assert diagnostic.docs_ref == "docs/06-api-reference/errors.md#hg-attempt-timeout"
    assert diagnostic.context.timeout_seconds == 0.05
    assert diagnostic.context.deadline_elapsed is True
    assert diagnostic.context.cancellation_requested is True


async def test_retry_window_expiry_during_work_maps_to_hg_retry_window_expired():
    import asyncio

    @node(
        output_name="value",
        retry=_policy(max_attempts=3, retry_window=0.05),
    )
    async def hang(x: int) -> int:
        await asyncio.Event().wait()
        return x

    with pytest.raises(RetryWindowExpiredError) as excinfo:
        await AsyncRunner().run(Graph([hang]), {"x": 1})

    diagnostic = _sole_evidence(excinfo.value).diagnostic
    assert diagnostic.code == "HG_RETRY_WINDOW_EXPIRED"
    assert diagnostic.context.retry_window_seconds == 0.05
    assert diagnostic.context.deadline_elapsed is True
    assert diagnostic.context.cancellation_requested is True


async def test_unsupported_timeout_maps_to_hg_timeout_unsupported():
    @node(output_name="value", timeout=1.0)
    def sync_with_timeout(x: int) -> int:
        return x

    with pytest.raises(IncompatibleRunnerError) as excinfo:
        SyncRunner().run(Graph([sync_with_timeout]), {"x": 1})

    assert excinfo.value.code == "HG_TIMEOUT_UNSUPPORTED"


async def test_persistence_failure_maps_to_hg_attempt_persistence_failed(family, make_sqlite, monkeypatch):
    import sqlite3

    cp = make_sqlite()

    @node(output_name="value", retry=_policy())
    def flaky(x: int) -> int:
        return x * 10

    def broken_begin_sync(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    async def broken_begin_async(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(type(cp), "begin_attempt_sync", broken_begin_sync, raising=True)
    monkeypatch.setattr(type(cp), "begin_attempt", broken_begin_async, raising=True)

    runner = _make_runner(family, checkpointer=cp)
    with pytest.raises(sqlite3.OperationalError) as excinfo:
        await _run(runner, Graph([flaky]), {"x": 1}, workflow_id="wf-persist")

    diagnostic = _sole_evidence(excinfo.value).diagnostic
    assert diagnostic.code == "HG_ATTEMPT_PERSISTENCE_FAILED"
    assert diagnostic.docs_ref == "docs/06-api-reference/errors.md#hg-attempt-persistence-failed"


# === Item 6: AttemptOutcomeUnknownError on resume ===


async def test_resume_after_crash_stranded_attempt_raises_outcome_unknown(family, make_sqlite):
    """A crash-stranded STARTED attempt must not silently re-run on resume."""
    cp = make_sqlite()

    class _SimulatedProcessDeath(BaseException):
        pass

    calls: list[int] = []

    # x has a default so the "new process" can resume the SAME workflow_id bare.
    @node(output_name="value", retry=_policy())
    def flaky(x: int = 1) -> int:
        calls.append(x)
        raise _SimulatedProcessDeath

    graph = Graph([flaky])
    with pytest.raises(_SimulatedProcessDeath):
        await _run(_make_runner(family, checkpointer=cp), graph, workflow_id="wf-unknown")

    assert calls == [1]
    series = await cp.get_open_attempt_series("wf-unknown", "flaky")
    assert series is not None
    records = await cp.get_attempt_records(series.id)
    assert [r.status for r in records] == [AttemptStatus.STARTED], "the reservation is stranded, not settled"

    # Resume in a "new process": the stranded reservation settles to
    # OUTCOME_UNKNOWN and the framework must refuse to silently re-run.
    resumed = _make_runner(family, checkpointer=make_sqlite())
    with pytest.raises(AttemptOutcomeUnknownError) as excinfo:
        await _run(resumed, graph, workflow_id="wf-unknown")

    assert calls == [1], "no silent re-invocation: external side effects may have completed"
    message = str(excinfo.value)
    assert "reconcile" in message.lower()
    assert "fork" in message.lower()

    diagnostic = _sole_evidence(excinfo.value).diagnostic
    assert diagnostic.code == "HG_ATTEMPT_OUTCOME_UNKNOWN"
    assert diagnostic.docs_ref == "docs/06-api-reference/errors.md#hg-attempt-outcome-unknown"

    records = await cp.get_attempt_records(series.id)
    assert [r.status for r in records] == [AttemptStatus.OUTCOME_UNKNOWN]
