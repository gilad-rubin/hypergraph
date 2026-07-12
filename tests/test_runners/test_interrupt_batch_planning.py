"""Interrupt batch planning happens once, in the runner (issue #147).

``plan_interrupt_batch`` isolates one interrupt per superstep BEFORE the
runner captures ``ready_node_names`` for checkpoint metadata, so the
checkpoint-recorded batch always equals the executed batch.
"""

from __future__ import annotations

from hypergraph import AsyncRunner, Graph, InterruptNode, node
from hypergraph.checkpointers import MemoryCheckpointer
from hypergraph.checkpointers.types import StepStatus
from hypergraph.runners._shared.helpers import plan_interrupt_batch


@node(output_name="side_out")
def side(x: int) -> int:
    return x + 1


@node(output_name="other_out")
def other(x: int) -> int:
    return x + 2


def _always_pause(draft: str) -> str | None:
    return None


def _make_interrupt() -> InterruptNode:
    return InterruptNode(_always_pause, name="approval", output_name="decision")


class TestPlanInterruptBatch:
    def test_no_interrupts_pass_through(self):
        graph = Graph([side, other])
        nodes = list(graph._nodes.values())
        assert plan_interrupt_batch(nodes) is nodes

    def test_interrupt_batch_isolates_first_interrupt(self):
        graph = Graph([side, _make_interrupt()])
        nodes = list(graph._nodes.values())
        planned = plan_interrupt_batch(nodes)
        assert len(planned) == 1
        assert planned[0].name == "approval"
        assert planned[0].is_interrupt


class TestCheckpointBatchMatchesExecutedBatch:
    async def test_recorded_superstep_batch_equals_executed_batch(self):
        """A ready non-interrupt sibling must not appear in the paused superstep's records."""
        checkpointer = MemoryCheckpointer()
        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([_make_interrupt(), side])

        result = await runner.run(
            graph,
            {"draft": "please review", "x": 1},
            workflow_id="wf-interrupt-batch",
        )

        assert result.paused
        steps = await checkpointer.get_steps("wf-interrupt-batch")
        superstep0 = [s for s in steps if s.superstep == 0]
        # Executed batch was exactly [approval]; the deferred sibling `side`
        # must not be recorded as part of this superstep.
        assert [s.node_name for s in superstep0] == ["approval"]
        assert superstep0[0].status == StepStatus.PAUSED

    async def test_nested_pause_preserves_completed_sibling_across_resume(self):
        """A sibling completed beside a pausing GraphNode must not run twice."""

        @node(output_name="side_out")
        async def side_effect(x: int) -> int:
            executions.append(x)
            return x + 1

        executions: list[int] = []
        checkpointer = MemoryCheckpointer()
        runner = AsyncRunner(checkpointer=checkpointer)
        inner = Graph([_make_interrupt()], name="review")
        graph = Graph([inner.as_node(name="review"), side_effect], name="outer")

        paused = await runner.run(
            graph,
            {"draft": "please review", "x": 1},
            workflow_id="wf-nested-pause-sibling",
        )

        assert paused.paused
        assert paused["side_out"] == 2
        assert executions == [1]
        paused_steps = await checkpointer.get_steps("wf-nested-pause-sibling")
        assert [(step.node_name, step.status) for step in paused_steps] == [
            ("review", StepStatus.PAUSED),
            ("side_effect", StepStatus.COMPLETED),
        ]

        assert paused.pause is not None
        resumed = await runner.run(
            graph,
            {paused.pause.response_key: "approved"},
            workflow_id="wf-nested-pause-sibling",
        )

        assert resumed.completed
        assert resumed["decision"] == "approved"
        assert resumed["side_out"] == 2
        assert executions == [1]
        all_steps = await checkpointer.get_steps("wf-nested-pause-sibling")
        assert [step.status for step in all_steps if step.node_name == "review"] == [
            StepStatus.PAUSED,
            StepStatus.COMPLETED,
        ]
        assert [step.status for step in all_steps if step.node_name == "side_effect"] == [
            StepStatus.COMPLETED,
        ]
