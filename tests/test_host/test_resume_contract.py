"""Issue #342 — what a durable resume carries, and what it restores.

The Host resumes an answered child by invoking the Graph runner on the SAME
workflow id with only ``{response_key: answer}``. These tests pin both
halves of that contract at the seam where it is decided:

- **What the caller must not do.** Resupplying the run's pinned start inputs
  as runtime overrides is refused — strict checkpoint resume forbids it.
- **What the store must therefore do.** A run's graph-boundary inputs are
  durable state in their own right (``runs.inputs_data``, written
  first-write-wins by ``create_run``), because step records fold node
  OUTPUTS only. ``get_checkpoint`` layers the folded state over those
  inputs, so a node placed after an interrupt can consume a raw graph input
  on resume and actually execute.

Everything here runs on a bare ``AsyncRunner`` / ``SyncRunner`` with a plain
``SqliteCheckpointer`` — no Host on the path. That is deliberate: the
guarantee belongs to the runner/checkpoint boundary, not to the Host.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

import pytest

from hypergraph import AsyncRunner, Graph, interrupt, node
from hypergraph.checkpointers import MemoryCheckpointer, SqliteCheckpointer
from hypergraph.runners import RunStatus

aiosqlite = pytest.importorskip("aiosqlite")

pytestmark = pytest.mark.host_batch_interrupt


@dataclass(frozen=True)
class Ask:
    answer_type: ClassVar[type] = str
    prompt: str
    options: tuple[str, ...] | None = None
    evidence: tuple = ()


def _graph(downstream_reads: str) -> Graph:
    """A three-node graph whose tail reads one of three input shapes."""

    @node(output_name="draft")
    def stage(work_item_id: str) -> str:
        return f"draft-{work_item_id}"

    @interrupt(answer_name="answer")
    def review(draft: str) -> Ask:
        return Ask(prompt="approve?")

    if downstream_reads == "answer_only":

        @node(output_name="final")
        def finish(answer: str) -> str:
            return f"F:{answer}"

    elif downstream_reads == "answer_plus_upstream_output":

        @node(output_name="final")
        def finish(answer: str, draft: str) -> str:
            return f"F:{answer}:{draft}"

    else:

        @node(output_name="final")
        def finish(answer: str, work_item_id: str) -> str:
            return f"F:{answer}:{work_item_id}"

    return Graph([stage, review, finish], name=downstream_reads)


@pytest.fixture
async def checkpointer(tmp_path):
    cp = SqliteCheckpointer(str(tmp_path / "cp.db"))
    yield cp
    await cp.close()


async def _pause_then_resume(checkpointer, graph: Graph, resume_values: dict[str, Any]):
    runner = AsyncRunner(checkpointer=checkpointer)
    first = await runner.run(graph, {"work_item_id": "w1"}, workflow_id="wf")
    assert first.paused
    return await runner.run(graph, resume_values, workflow_id="wf")


class TestAPostInterruptNodeCanReadAGraphInput:
    """THE regression: the exact success path issue #342's review demanded."""

    async def test_a_tail_reading_a_raw_start_input_runs_on_resume(self, checkpointer):
        runner = AsyncRunner(checkpointer=checkpointer)

        first = await runner.run(
            _graph("answer_plus_start_input"),
            {"work_item_id": "w1"},
            workflow_id="wf",
        )
        assert first.paused

        resumed = await runner.run(
            _graph("answer_plus_start_input"),
            {"answer": "yes"},
            workflow_id="wf",
        )

        assert resumed.status is RunStatus.COMPLETED
        assert resumed["final"] == "F:yes:w1"

    async def test_the_sync_store_mirror_restores_the_same_input(self, tmp_path):
        """Sync/async parity for the durable half.

        ``SyncRunner`` refuses interrupts outright, so the synchronous side
        of this guarantee lives at the store: ``create_run_sync`` writes the
        inputs first-write-wins and ``checkpoint()`` layers state over them.
        """
        cp = SqliteCheckpointer(str(tmp_path / "sync.db"))
        try:
            graph = _graph("answer_plus_start_input")
            cp.create_run_sync("wf-sync", graph_name=graph.name, inputs={"work_item_id": "w9"})
            assert cp.checkpoint("wf-sync").values["work_item_id"] == "w9"
            # A later upsert (what a resume does) must not overwrite them.
            cp.create_run_sync("wf-sync", graph_name=graph.name, inputs={"work_item_id": "CLOBBERED"})
            assert cp.get_run_inputs_sync("wf-sync") == {"work_item_id": "w9"}
            assert cp.checkpoint("wf-sync").values["work_item_id"] == "w9"
        finally:
            await cp.close()

    @pytest.mark.parametrize("shape", ["answer_only", "answer_plus_upstream_output"])
    async def test_the_output_threaded_shapes_still_resume(self, checkpointer, shape):
        resumed = await _pause_then_resume(checkpointer, _graph(shape), {"answer": "yes"})

        assert resumed.status is RunStatus.COMPLETED
        assert resumed["final"].startswith("F:yes")

    async def test_every_active_node_produced_its_output(self, checkpointer):
        """No silent no-op: COMPLETED means the tail actually ran."""
        resumed = await _pause_then_resume(checkpointer, _graph("answer_plus_start_input"), {"answer": "ok"})

        assert resumed.status is RunStatus.COMPLETED
        assert resumed.get("final") is not None


class TestRunInputsAreDurableState:
    async def test_a_checkpoint_restores_inputs_under_the_folded_state(self, checkpointer):
        await AsyncRunner(checkpointer=checkpointer).run(_graph("answer_only"), {"work_item_id": "w1"}, workflow_id="wf")

        checkpoint = await checkpointer.get_checkpoint("wf")

        # `draft` is a node output; `work_item_id` is the run's own input.
        # Both are restorable, which is what a checkpoint means.
        assert checkpoint.values == {"work_item_id": "w1", "draft": "draft-w1"}
        assert await checkpointer.get_run_inputs("wf") == {"work_item_id": "w1"}

    async def test_get_state_still_folds_step_outputs_only(self, checkpointer):
        """The journal projection is unchanged; only restoration layers inputs."""
        await AsyncRunner(checkpointer=checkpointer).run(_graph("answer_only"), {"work_item_id": "w1"}, workflow_id="wf")

        assert await checkpointer.get_state("wf") == {"draft": "draft-w1"}

    async def test_a_resume_never_overwrites_the_stored_inputs(self, checkpointer):
        await _pause_then_resume(checkpointer, _graph("answer_only"), {"answer": "yes"})

        # The resume upsert carried only the answer port; the originals stand.
        assert await checkpointer.get_run_inputs("wf") == {"work_item_id": "w1"}

    async def test_the_memory_backend_mirrors_the_rule(self):
        cp = MemoryCheckpointer()
        graph = _graph("answer_plus_start_input")

        first = await AsyncRunner(checkpointer=cp).run(graph, {"work_item_id": "m1"}, workflow_id="wf")
        assert first.paused
        resumed = await AsyncRunner(checkpointer=cp).run(graph, {"answer": "yes"}, workflow_id="wf")

        assert resumed["final"] == "F:yes:m1"
        assert await cp.get_run_inputs("wf") == {"work_item_id": "m1"}

    async def test_a_legacy_run_without_stored_inputs_still_resumes(self, checkpointer):
        """Databases written before `runs.inputs_data` existed keep working."""
        graph = _graph("answer_only")
        first = await AsyncRunner(checkpointer=checkpointer).run(graph, {"work_item_id": "w1"}, workflow_id="wf")
        assert first.paused

        await checkpointer._ensure_db()
        await checkpointer._db.execute("UPDATE runs SET inputs_data = NULL WHERE id = ?", ("wf",))
        await checkpointer._db.commit()

        resumed = await AsyncRunner(checkpointer=checkpointer).run(graph, {"answer": "yes"}, workflow_id="wf")
        assert resumed.status is RunStatus.COMPLETED


class TestTheResumePayloadStaysNarrow:
    async def test_resupplying_a_pinned_start_input_is_refused(self, checkpointer):
        """Why the worker sends the answer port alone and nothing else."""
        from hypergraph.exceptions import InputOverrideRequiresForkError

        with pytest.raises(InputOverrideRequiresForkError):
            await _pause_then_resume(checkpointer, _graph("answer_only"), {"answer": "yes", "work_item_id": "w1"})

    async def test_a_fork_inherits_the_source_run_inputs(self, checkpointer):
        """A fork must be independently restorable, not a pointer to its source."""
        graph = _graph("answer_plus_start_input")
        runner = AsyncRunner(checkpointer=checkpointer)
        first = await runner.run(graph, {"work_item_id": "w1"}, workflow_id="wf")
        assert first.paused

        _forked_id, checkpoint = await checkpointer.fork_workflow_async("wf")
        assert checkpoint.values["work_item_id"] == "w1"

        forked = await runner.run(graph, {"answer": "yes"}, fork_from="wf")

        assert forked["final"] == "F:yes:w1"
        # The fork carries its own copy, so IT can be resumed again later.
        assert await checkpointer.get_run_inputs(forked.workflow_id) == {"work_item_id": "w1"}
