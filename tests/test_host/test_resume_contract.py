"""Issue #342 — what a durable resume may and may not carry.

The Host resumes an answered child by invoking the Graph runner on the SAME
workflow id with only ``{response_key: answer}``. These tests pin both
halves of that contract at the seam where it is decided:

- **What it must not do.** It must never resupply the submission's pinned
  start inputs as runtime overrides — strict checkpoint resume forbids it.
- **What that costs.** A checkpoint stores node OUTPUTS, so a raw
  graph-boundary input is not restorable. A post-interrupt node reading one
  therefore cannot become ready on resume. That is pre-existing Tier-0
  behavior (reproduced below with a bare ``AsyncRunner`` and a plain
  ``SqliteCheckpointer`` — no Host involved), and it is the reason the
  ingestion fixture threads ``validated_id`` rather than ``work_item_id``.

Pinning the limitation here means a future change that fixes it — or one
that quietly widens the Host's resume payload to paper over it — has to come
past this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

import pytest

from hypergraph import AsyncRunner, Graph, interrupt, node
from hypergraph.checkpointers import SqliteCheckpointer

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

    @interrupt(answer_name="ans")
    def review(draft: str) -> Ask:
        return Ask(prompt="approve?")

    if downstream_reads == "answer_only":

        @node(output_name="final")
        def finish(ans: str) -> str:
            return f"F:{ans}"

    elif downstream_reads == "answer_plus_upstream_output":

        @node(output_name="final")
        def finish(ans: str, draft: str) -> str:
            return f"F:{ans}:{draft}"

    else:

        @node(output_name="final")
        def finish(ans: str, work_item_id: str) -> str:
            return f"F:{ans}:{work_item_id}"

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


class TestTheAnswerPortIsEnoughForOutputThreadedGraphs:
    @pytest.mark.parametrize("shape", ["answer_only", "answer_plus_upstream_output"])
    async def test_only_the_settled_answer_resumes_the_run(self, checkpointer, shape):
        resumed = await _pause_then_resume(checkpointer, _graph(shape), {"ans": "yes"})

        assert resumed.status.value == "completed"
        assert resumed["final"].startswith("F:yes")

    async def test_resupplying_a_pinned_start_input_is_refused(self, checkpointer):
        """Why the worker sends the answer port alone and nothing else."""
        from hypergraph.exceptions import InputOverrideRequiresForkError

        with pytest.raises(InputOverrideRequiresForkError):
            await _pause_then_resume(checkpointer, _graph("answer_only"), {"ans": "yes", "work_item_id": "w1"})


class TestBoundaryInputsAreNotRestorable:
    async def test_a_checkpoint_stores_node_outputs_not_graph_inputs(self, checkpointer):
        runner = AsyncRunner(checkpointer=checkpointer)
        await runner.run(_graph("answer_only"), {"work_item_id": "w1"}, workflow_id="wf")

        checkpoint = await checkpointer.get_checkpoint("wf")

        # `draft` is produced by a node, so it survives. `work_item_id` was
        # only ever consumed, so it is simply not there.
        assert checkpoint.values == {"draft": "draft-w1"}
        assert "work_item_id" not in checkpoint.values

    async def test_a_tail_reading_a_raw_start_input_cannot_run_on_resume(self, checkpointer):
        """Pre-existing Tier-0 limitation — no Host code on this path.

        The run reports COMPLETED and the node's output is simply absent,
        which is why the ingestion fixture threads a node output
        (``validated_id``) into every post-interrupt node instead.
        """
        resumed = await _pause_then_resume(checkpointer, _graph("answer_plus_start_input"), {"ans": "yes"})

        assert resumed.status.value == "completed"
        assert resumed.get("final") is None
