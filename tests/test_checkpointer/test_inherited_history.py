"""A fork or retry carries the work it inherits in its own record.

The reviewed persistence spec makes forks "pure data copies — no references
or pointers to the source ... each workflow is self-contained". A run started
from another run's checkpoint (``retry_from=``, ``fork_from=``, or an explicit
``checkpoint=``) therefore writes the source's COMPLETED steps under its own
id when it is created, beside the source's inputs it already stores.

Without that, the new run's record held only the steps it executed itself:

- its folded state, and so ``RunHomeClient.result()``, lost every output the
  source had already saved as soon as the retry produced anything of its own;
- its checkpoint was no longer "the whole restorable state of a run", so a
  retry OF that retry ran the source's finished steps again, side effects
  and all, and still reported COMPLETED.

Pinned here, on SQLite (async and sync) and the in-memory backend: a retry of
a retry repeats nothing its lineage finished; the retry's own state and
checkpoint hold what it inherited; only completed work is inherited, never
the source's failures; a fork taken at a superstep inherits only that far;
an inherited step keeps no link to the source's attempt ledger.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from hypergraph import AsyncRunner, Graph, RetryPolicy, RunStatus, SyncRunner, node
from hypergraph.checkpointers import MemoryCheckpointer, SqliteCheckpointer, StepRecord, StepStatus

pytest.importorskip("aiosqlite")


class _Flaky(Exception):
    """The transient failure an ingestion step raises once."""


def _ingestion(calls: dict[str, int], fail_once: set[str]) -> Graph:
    """stage -> materialize -> publish, each node failing on its first call if listed.

    The shape of a knowledge-base upload: staging writes a version, then the
    pages are materialized, then the version is published. Every call is
    counted, so a step the lineage already finished can be caught running again.
    """

    def attempt(name: str) -> None:
        calls[name] += 1
        if name in fail_once and calls[name] == 1:
            raise _Flaky(f"{name} is down")

    @node(output_name="staged")
    def stage(doc: str) -> dict[str, str]:
        attempt("stage")
        return {"version_id": f"v-{doc}"}

    @node(output_name="materialization")
    def materialize(staged: dict[str, str]) -> dict[str, Any]:
        attempt("materialize")
        return {"version_id": staged["version_id"], "rows": 3}

    @node(output_name="published")
    def publish(materialization: dict[str, Any]) -> dict[str, int]:
        attempt("publish")
        return {"pages": materialization["rows"]}

    return Graph([stage, materialize, publish], name="ingest")


def _calls() -> dict[str, int]:
    return {"stage": 0, "materialize": 0, "publish": 0}


@pytest_asyncio.fixture(params=["sqlite", "memory"])
async def checkpointer(request, tmp_path) -> AsyncIterator[Any]:
    if request.param == "memory":
        yield MemoryCheckpointer()
        return
    cp = SqliteCheckpointer(str(tmp_path / "inherit.db"))
    yield cp
    await cp.close()


@pytest.fixture
def sync_checkpointer(tmp_path):
    cp = SqliteCheckpointer(str(tmp_path / "inherit-sync.db"))
    cp._sync_db()
    yield cp
    if cp._sync_conn:
        cp._sync_conn.close()


async def _failed_twice(checkpointer: Any, calls: dict[str, int]) -> tuple[Graph, AsyncRunner, Any, Any]:
    """Source fails at materialize; its retry materializes and fails at publish."""
    graph = _ingestion(calls, fail_once={"materialize", "publish"})
    runner = AsyncRunner(checkpointer=checkpointer)
    source = await runner.run(graph, {"doc": "1002"}, workflow_id="upload-1002", error_handling="continue")
    assert source.status is RunStatus.FAILED
    retry = await runner.run(graph, retry_from="upload-1002", workflow_id="upload-1002-retry-1", error_handling="continue")
    assert retry.status is RunStatus.FAILED
    assert calls == {"stage": 1, "materialize": 2, "publish": 1}
    return graph, runner, source, retry


class TestRetryOfARetry:
    async def test_repeats_nothing_its_lineage_finished(self, checkpointer):
        calls = _calls()
        graph, runner, _, _ = await _failed_twice(checkpointer, calls)

        again = await runner.run(graph, retry_from="upload-1002-retry-1", workflow_id="upload-1002-retry-2")

        assert again.status is RunStatus.COMPLETED
        assert calls == {"stage": 1, "materialize": 2, "publish": 2}, "stage and materialize finished earlier in the lineage"
        assert again.values["published"] == {"pages": 3}
        assert await checkpointer.get_state("upload-1002-retry-2") == {
            "staged": {"version_id": "v-1002"},
            "materialization": {"version_id": "v-1002", "rows": 3},
            "published": {"pages": 3},
        }

    def test_sync_mirror_repeats_nothing_its_lineage_finished(self, sync_checkpointer):
        calls = _calls()
        graph = _ingestion(calls, fail_once={"materialize", "publish"})
        runner = SyncRunner(checkpointer=sync_checkpointer)
        runner.run(graph, {"doc": "1002"}, workflow_id="upload-1002", error_handling="continue")
        runner.run(graph, retry_from="upload-1002", workflow_id="upload-1002-retry-1", error_handling="continue")

        again = runner.run(graph, retry_from="upload-1002-retry-1", workflow_id="upload-1002-retry-2")

        assert again.status is RunStatus.COMPLETED
        assert calls == {"stage": 1, "materialize": 2, "publish": 2}
        assert sorted(sync_checkpointer.state("upload-1002-retry-2")) == ["materialization", "published", "staged"]


class TestTheRetrysOwnRecord:
    async def test_holds_what_it_inherited_beside_what_it_made(self, checkpointer):
        await _failed_twice(checkpointer, _calls())

        state = await checkpointer.get_state("upload-1002-retry-1")
        assert state == {"staged": {"version_id": "v-1002"}, "materialization": {"version_id": "v-1002", "rows": 3}}
        checkpoint = await checkpointer.get_checkpoint("upload-1002-retry-1")
        assert checkpoint.values == {"doc": "1002", **state}
        completed = [step.node_name for step in checkpoint.steps if step.status is StepStatus.COMPLETED]
        assert completed == ["stage", "materialize"]
        assert {step.run_id for step in checkpoint.steps} == {"upload-1002-retry-1"}, "stored under the retry's own id"

    async def test_inherits_completed_work_never_the_sources_failure(self, checkpointer):
        calls = _calls()
        graph = _ingestion(calls, fail_once={"materialize"})
        runner = AsyncRunner(checkpointer=checkpointer)
        await runner.run(graph, {"doc": "7"}, workflow_id="upload-7", error_handling="continue")

        retry = await runner.run(graph, retry_from="upload-7", workflow_id="upload-7-retry-1")

        assert retry.status is RunStatus.COMPLETED
        steps = await checkpointer.get_steps("upload-7-retry-1")
        assert [(step.node_name, step.status) for step in steps] == [
            ("stage", StepStatus.COMPLETED),
            ("materialize", StepStatus.COMPLETED),
            ("publish", StepStatus.COMPLETED),
        ], "the source's failed materialize is its own attempt, not the retry's"
        assert calls == {"stage": 1, "materialize": 2, "publish": 1}

    async def test_a_fork_at_a_superstep_inherits_only_that_far(self, checkpointer):
        calls = _calls()
        graph = _ingestion(calls, fail_once=set())
        runner = AsyncRunner(checkpointer=checkpointer)
        await runner.run(graph, {"doc": "9"}, workflow_id="upload-9")
        first = (await checkpointer.get_steps("upload-9"))[0]
        assert first.node_name == "stage"

        checkpoint = await checkpointer.get_checkpoint("upload-9", superstep=first.superstep)
        forked = await runner.run(graph, checkpoint=checkpoint, workflow_id="upload-9-fork")

        assert forked.status is RunStatus.COMPLETED
        assert calls == {"stage": 1, "materialize": 2, "publish": 2}
        steps = await checkpointer.get_steps("upload-9-fork")
        assert [step.node_name for step in steps] == ["stage", "materialize", "publish"]
        assert sorted(await checkpointer.get_state("upload-9-fork")) == ["materialization", "published", "staged"]


class TestNoLinkToTheSourcesLedger:
    async def test_an_inherited_step_names_no_attempt_series(self, checkpointer):
        """A step's attempt series belongs to the run that made the attempts.

        Retention compaction deletes the closed series a dropped step links
        to, by id; an inherited row that kept the source's link would let the
        retry's compaction delete the SOURCE's attempt ledger.
        """
        calls = {"n": 0}

        @node(output_name="staged", retry=RetryPolicy(max_attempts=2, retry_on=(_Flaky,), initial_delay=0.001))
        def stage(doc: str) -> str:
            calls["n"] += 1
            return f"v-{doc}"

        @node(output_name="published")
        def publish(staged: str, ready: bool) -> str:
            if not ready:
                raise _Flaky("publish guard")
            return staged

        graph = Graph([stage, publish], name="ledger")
        runner = AsyncRunner(checkpointer=checkpointer)
        await runner.run(graph, {"doc": "3", "ready": False}, workflow_id="ledger-src", error_handling="continue")
        (source_stage,) = [step for step in await checkpointer.get_steps("ledger-src") if step.node_name == "stage"]
        assert source_stage.attempt_series_id is not None, "the precondition: the source's step links its ledger"

        forked = await runner.run(graph, {"ready": True}, fork_from="ledger-src", workflow_id="ledger-fork")

        assert forked.status is RunStatus.COMPLETED
        assert calls["n"] == 1
        (inherited,) = [step for step in await checkpointer.get_steps("ledger-fork") if step.node_name == "stage"]
        assert inherited.attempt_series_id is None
        assert inherited.values == source_stage.values


def _inherited(run_id: str, *, attempt_series_id: str | None = None) -> list[StepRecord]:
    """Two inherited rows; the second can be made to break its foreign key."""
    return [
        StepRecord(
            run_id=run_id, superstep=0, node_name="stage", index=0, status=StepStatus.COMPLETED, input_versions={"doc": 1}, values={"staged": "v-1"}
        ),
        StepRecord(
            run_id=run_id,
            superstep=1,
            node_name="materialize",
            index=1,
            status=StepStatus.COMPLETED,
            input_versions={"staged": 1},
            values={"materialization": 3},
            attempt_series_id=attempt_series_id,
        ),
    ]


class TestWrittenWithTheRow:
    """A run killed half-way through writing its inheritance would resume from
    part of it and run the rest again, so the row and its history are one commit."""

    async def test_the_row_and_its_history_commit_together(self, tmp_path):
        cp = SqliteCheckpointer(str(tmp_path / "atomic.db"))
        try:
            await cp.create_run("fork-ok", graph_name="ingest", history=_inherited("fork-ok"))
            assert await cp.get_state("fork-ok") == {"staged": "v-1", "materialization": 3}

            with pytest.raises(sqlite3.IntegrityError):
                await cp.create_run("fork-bad", graph_name="ingest", history=_inherited("fork-bad", attempt_series_id="no-such-series"))
            assert await cp.get_run_async("fork-bad") is None, "no row without its history"
            assert await cp.get_steps("fork-bad") == []
        finally:
            await cp.close()

    def test_sync_mirror_commits_together(self, sync_checkpointer):
        sync_checkpointer.create_run_sync("fork-ok", graph_name="ingest", history=_inherited("fork-ok"))
        assert sync_checkpointer.state("fork-ok") == {"staged": "v-1", "materialization": 3}

        with pytest.raises(sqlite3.IntegrityError):
            sync_checkpointer.create_run_sync("fork-bad", graph_name="ingest", history=_inherited("fork-bad", attempt_series_id="no-such-series"))
        assert sync_checkpointer.get_run("fork-bad") is None
        assert list(sync_checkpointer.steps("fork-bad")) == []
