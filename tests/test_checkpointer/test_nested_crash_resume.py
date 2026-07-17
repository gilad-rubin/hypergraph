"""Crash-window recovery for nested GraphNodes (issue #235).

Witness (reproduced during the #187 investigation): a crash lands between a
child workflow committing terminal COMPLETED and the parent writing its
GraphNode StepRecord. On resume the parent must RESTORE the child's persisted
outputs and commit the missing parent step — not re-invoke the terminal child
(which raises ``WorkflowAlreadyCompletedError``) and never silently restore a
FAILED child as success.
"""

import pytest

from hypergraph import AsyncRunner, Graph, SyncRunner, node
from hypergraph.checkpointers import SqliteCheckpointer, WorkflowStatus
from hypergraph.checkpointers.types import StepStatus
from hypergraph.events import EventProcessor
from hypergraph.events.types import NodeEndEvent, NodeStartEvent, RunStartEvent
from hypergraph.runners._shared.results import RunStatus

aiosqlite = pytest.importorskip("aiosqlite")

CRASH_MESSAGE = "simulated crash before parent step write"


class EventCollector(EventProcessor):
    """Collect execution events without adding behavior to the runner."""

    def __init__(self):
        self.events: list[object] = []

    def on_event(self, event):
        self.events.append(event)

    def of_type(self, event_type):
        return [event for event in self.events if isinstance(event, event_type)]


class CrashingStepCheckpointer(SqliteCheckpointer):
    """Raise on targeted (run_id, node_name) step writes while armed.

    Simulates the crash window: the child workflow has already committed
    terminal status, but the parent's StepRecord for the GraphNode is never
    persisted. Disarm (``armed = False``) to model a healthy process resuming
    against the same database.
    """

    def __init__(self, path: str, targets: set[tuple[str, str]]):
        super().__init__(path, durability="sync")
        self.targets = targets
        self.armed = True

    def _hit(self, record) -> bool:
        return self.armed and (record.run_id, record.node_name) in self.targets

    async def save_step(self, record) -> None:
        if self._hit(record):
            raise RuntimeError(CRASH_MESSAGE)
        await super().save_step(record)

    def save_step_sync(self, record) -> None:
        if self._hit(record):
            raise RuntimeError(CRASH_MESSAGE)
        super().save_step_sync(record)


def build_nested_graph():
    """parent: prepare -> child_wf(double) -> consume, with invocation counters."""
    counters = {"prepare": 0, "double": 0, "consume": 0}

    @node(output_name="prepared")
    def prepare(x: int) -> int:
        counters["prepare"] += 1
        return x + 100

    @node(output_name="doubled")
    def double(prepared: int) -> int:
        counters["double"] += 1
        return prepared * 2

    @node(output_name="final")
    def consume(doubled: int) -> int:
        counters["consume"] += 1
        return doubled + 1

    child = Graph(nodes=[double], name="child")
    parent = Graph(nodes=[prepare, child.as_node(name="child_wf"), consume], name="parent")
    return parent, counters


def build_two_level_graph():
    """outer: outer_prep -> mid_wf(mid_prep -> grand_wf(innermost)) -> outer_consume."""
    counters = {"outer_prep": 0, "mid_prep": 0, "innermost": 0, "outer_consume": 0}

    @node(output_name="seed")
    def outer_prep(x: int) -> int:
        counters["outer_prep"] += 1
        return x + 1

    @node(output_name="mid_ready")
    def mid_prep(seed: int) -> int:
        counters["mid_prep"] += 1
        return seed * 10

    @node(output_name="tripled")
    def innermost(mid_ready: int) -> int:
        counters["innermost"] += 1
        return mid_ready * 3

    @node(output_name="final")
    def outer_consume(tripled: int) -> int:
        counters["outer_consume"] += 1
        return tripled + 7

    grand = Graph(nodes=[innermost], name="grand")
    mid = Graph(nodes=[mid_prep, grand.as_node(name="grand_wf")], name="mid")
    outer = Graph(nodes=[outer_prep, mid.as_node(name="mid_wf"), outer_consume], name="outer")
    return outer, counters


def build_failing_child_graph():
    """parent whose nested child fails deterministically.

    The failing node's input comes from a completed inner step (``staged``),
    so the child itself is mechanically resumable — resume re-executes only
    the failed node, which fails again. That isolates B6 on failure surfacing
    rather than on child-input recovery.
    """
    counters = {"boom": 0}

    @node(output_name="prepared")
    def prepare(x: int) -> int:
        return x + 100

    @node(output_name="staged")
    def stage(prepared: int) -> int:
        return prepared + 1

    @node(output_name="doubled")
    def boom(staged: int) -> int:
        counters["boom"] += 1
        raise ValueError("child exploded")

    @node(output_name="final")
    def consume(doubled: int) -> int:
        return doubled + 1

    child = Graph(nodes=[stage, boom], name="child")
    parent = Graph(nodes=[prepare, child.as_node(name="child_wf"), consume], name="parent")
    return parent, counters


class TestAsyncNestedCrashResume:
    async def test_resume_restores_completed_child_after_parent_step_crash(self, tmp_path):
        """B1/B2/B3: resume restores the child's outputs instead of re-invoking."""
        parent, counters = build_nested_graph()
        cp = CrashingStepCheckpointer(str(tmp_path / "test.db"), {("wf", "child_wf")})
        try:
            runner = AsyncRunner(checkpointer=cp)
            with pytest.raises(RuntimeError, match=CRASH_MESSAGE):
                await runner.run(parent, {"x": 5}, workflow_id="wf")

            # The witness: child terminal COMPLETED, parent GraphNode step missing.
            child_run = await cp.get_run_async("wf/child_wf")
            assert child_run is not None
            assert child_run.status is WorkflowStatus.COMPLETED
            crash_steps = await cp.get_steps("wf")
            assert "child_wf" not in {step.node_name for step in crash_steps}
            assert counters == {"prepare": 1, "double": 1, "consume": 0}

            cp.armed = False
            result = await runner.run(parent, workflow_id="wf")

            # B3: the resumed run carries the child's outputs.
            assert result.values["doubled"] == 210
            assert result.values["final"] == 211
            # B2: the child's inner node did NOT re-execute on resume.
            assert counters["double"] == 1
            # Replayed upstream node did not re-run; downstream ran exactly once.
            assert counters["prepare"] == 1
            assert counters["consume"] == 1

            # B3: the missing parent step is committed as COMPLETED.
            steps = {step.node_name: step for step in await cp.get_steps("wf")}
            child_step = steps["child_wf"]
            assert child_step.status is StepStatus.COMPLETED
            assert child_step.values == {"doubled": 210}
            assert child_step.child_run_id == "wf/child_wf"
            parent_run = await cp.get_run_async("wf")
            assert parent_run is not None
            assert parent_run.status is WorkflowStatus.COMPLETED
        finally:
            await cp.close()

    async def test_resume_emits_no_child_execution_events(self, tmp_path):
        """B7: the restore path emits zero fresh child-node execution events."""
        parent, counters = build_nested_graph()
        cp = CrashingStepCheckpointer(str(tmp_path / "test.db"), {("wf", "child_wf")})
        try:
            runner = AsyncRunner(checkpointer=cp)
            with pytest.raises(RuntimeError, match=CRASH_MESSAGE):
                await runner.run(parent, {"x": 5}, workflow_id="wf")
            cp.armed = False

            collector = EventCollector()
            result = await runner.run(parent, workflow_id="wf", event_processors=[collector])
            assert result.values["final"] == 211

            child_starts = [event for event in collector.of_type(NodeStartEvent) if event.workflow_id == "wf/child_wf" or event.node_name == "double"]
            child_ends = [event for event in collector.of_type(NodeEndEvent) if event.workflow_id == "wf/child_wf" or event.node_name == "double"]
            child_run_starts = [event for event in collector.of_type(RunStartEvent) if event.workflow_id == "wf/child_wf"]
            assert child_starts == []
            assert child_ends == []
            assert child_run_starts == []
            # The parent GraphNode's own commit stays visible (truthful restore).
            parent_ends = [event for event in collector.of_type(NodeEndEvent) if event.node_name == "child_wf"]
            assert len(parent_ends) == 1
        finally:
            await cp.close()

    async def test_two_level_crash_restores_grandchild(self, tmp_path):
        """B5: grandchild terminal COMPLETED with both ancestor steps missing."""
        outer, counters = build_two_level_graph()
        targets = {("wf2/mid_wf", "grand_wf"), ("wf2", "mid_wf")}
        cp = CrashingStepCheckpointer(str(tmp_path / "test.db"), targets)
        try:
            runner = AsyncRunner(checkpointer=cp)
            with pytest.raises(RuntimeError, match=CRASH_MESSAGE):
                await runner.run(outer, {"x": 2}, workflow_id="wf2")

            grand_run = await cp.get_run_async("wf2/mid_wf/grand_wf")
            assert grand_run is not None
            assert grand_run.status is WorkflowStatus.COMPLETED
            assert "grand_wf" not in {step.node_name for step in await cp.get_steps("wf2/mid_wf")}
            assert "mid_wf" not in {step.node_name for step in await cp.get_steps("wf2")}
            assert counters == {"outer_prep": 1, "mid_prep": 1, "innermost": 1, "outer_consume": 0}

            cp.armed = False
            result = await runner.run(outer, workflow_id="wf2")

            assert result.values["final"] == 97  # ((2+1)*10)*3 + 7
            assert counters["innermost"] == 1  # grandchild not re-invoked
            assert counters["mid_prep"] == 1
            assert counters["outer_prep"] == 1
            assert counters["outer_consume"] == 1

            mid_steps = {step.node_name: step for step in await cp.get_steps("wf2/mid_wf")}
            assert mid_steps["grand_wf"].status is StepStatus.COMPLETED
            outer_steps = {step.node_name: step for step in await cp.get_steps("wf2")}
            assert outer_steps["mid_wf"].status is StepStatus.COMPLETED
            for run_id in ("wf2", "wf2/mid_wf", "wf2/mid_wf/grand_wf"):
                run = await cp.get_run_async(run_id)
                assert run is not None
                assert run.status is WorkflowStatus.COMPLETED
        finally:
            await cp.close()

    @pytest.mark.parametrize("error_handling", ["raise", "continue"])
    async def test_resume_does_not_mask_failed_child(self, tmp_path, error_handling):
        """B6: a FAILED terminal child surfaces its failure — no restore-as-success."""
        parent, counters = build_failing_child_graph()
        cp = SqliteCheckpointer(str(tmp_path / "test.db"), durability="sync")
        try:
            runner = AsyncRunner(checkpointer=cp)
            if error_handling == "raise":
                with pytest.raises(ValueError, match="child exploded"):
                    await runner.run(parent, {"x": 5}, workflow_id="wf", error_handling=error_handling)
            else:
                first = await runner.run(parent, {"x": 5}, workflow_id="wf", error_handling=error_handling)
                assert first.status is RunStatus.FAILED

            child_run = await cp.get_run_async("wf/child_wf")
            assert child_run is not None
            assert child_run.status is WorkflowStatus.FAILED
            assert counters["boom"] == 1

            if error_handling == "raise":
                with pytest.raises(ValueError, match="child exploded"):
                    await runner.run(parent, workflow_id="wf", error_handling=error_handling)
            else:
                result = await runner.run(parent, workflow_id="wf", error_handling=error_handling)
                assert result.status is RunStatus.FAILED
                assert isinstance(result.error, ValueError)
                assert "doubled" not in result.values
                assert "final" not in result.values

            # Existing semantics: the failed child re-executes (and fails again).
            assert counters["boom"] == 2
            child_run = await cp.get_run_async("wf/child_wf")
            assert child_run is not None
            assert child_run.status is WorkflowStatus.FAILED
        finally:
            await cp.close()


class TestSyncNestedCrashResume:
    """B4: sync-runner parity for the crash-window restore."""

    def _sync_cp(self, tmp_path, targets):
        cp = CrashingStepCheckpointer(str(tmp_path / "test.db"), targets)
        cp._sync_db()  # triggers schema creation
        return cp

    def test_resume_restores_completed_child_after_parent_step_crash(self, tmp_path):
        parent, counters = build_nested_graph()
        cp = self._sync_cp(tmp_path, {("wf", "child_wf")})
        try:
            runner = SyncRunner(checkpointer=cp)
            with pytest.raises(RuntimeError, match=CRASH_MESSAGE):
                runner.run(parent, {"x": 5}, workflow_id="wf")

            child_run = cp.get_run("wf/child_wf")
            assert child_run is not None
            assert child_run.status is WorkflowStatus.COMPLETED
            assert "child_wf" not in {step.node_name for step in cp.steps("wf")}
            assert counters == {"prepare": 1, "double": 1, "consume": 0}

            cp.armed = False
            result = runner.run(parent, workflow_id="wf")

            assert result.values["doubled"] == 210
            assert result.values["final"] == 211
            assert counters["double"] == 1
            assert counters["prepare"] == 1
            assert counters["consume"] == 1

            steps = {step.node_name: step for step in cp.steps("wf")}
            child_step = steps["child_wf"]
            assert child_step.status is StepStatus.COMPLETED
            assert child_step.values == {"doubled": 210}
            assert child_step.child_run_id == "wf/child_wf"
            parent_run = cp.get_run("wf")
            assert parent_run is not None
            assert parent_run.status is WorkflowStatus.COMPLETED
        finally:
            if cp._sync_conn:
                cp._sync_conn.close()

    def test_resume_emits_no_child_execution_events(self, tmp_path):
        parent, counters = build_nested_graph()
        cp = self._sync_cp(tmp_path, {("wf", "child_wf")})
        try:
            runner = SyncRunner(checkpointer=cp)
            with pytest.raises(RuntimeError, match=CRASH_MESSAGE):
                runner.run(parent, {"x": 5}, workflow_id="wf")
            cp.armed = False

            collector = EventCollector()
            result = runner.run(parent, workflow_id="wf", event_processors=[collector])
            assert result.values["final"] == 211

            child_starts = [event for event in collector.of_type(NodeStartEvent) if event.workflow_id == "wf/child_wf" or event.node_name == "double"]
            child_ends = [event for event in collector.of_type(NodeEndEvent) if event.workflow_id == "wf/child_wf" or event.node_name == "double"]
            child_run_starts = [event for event in collector.of_type(RunStartEvent) if event.workflow_id == "wf/child_wf"]
            assert child_starts == []
            assert child_ends == []
            assert child_run_starts == []
            parent_ends = [event for event in collector.of_type(NodeEndEvent) if event.node_name == "child_wf"]
            assert len(parent_ends) == 1
        finally:
            if cp._sync_conn:
                cp._sync_conn.close()

    def test_two_level_crash_restores_grandchild(self, tmp_path):
        outer, counters = build_two_level_graph()
        cp = self._sync_cp(tmp_path, {("wf2/mid_wf", "grand_wf"), ("wf2", "mid_wf")})
        try:
            runner = SyncRunner(checkpointer=cp)
            with pytest.raises(RuntimeError, match=CRASH_MESSAGE):
                runner.run(outer, {"x": 2}, workflow_id="wf2")

            grand_run = cp.get_run("wf2/mid_wf/grand_wf")
            assert grand_run is not None
            assert grand_run.status is WorkflowStatus.COMPLETED
            assert counters == {"outer_prep": 1, "mid_prep": 1, "innermost": 1, "outer_consume": 0}

            cp.armed = False
            result = runner.run(outer, workflow_id="wf2")

            assert result.values["final"] == 97
            assert counters["innermost"] == 1
            assert counters["mid_prep"] == 1
            assert counters["outer_consume"] == 1

            mid_steps = {step.node_name: step for step in cp.steps("wf2/mid_wf")}
            assert mid_steps["grand_wf"].status is StepStatus.COMPLETED
            outer_steps = {step.node_name: step for step in cp.steps("wf2")}
            assert outer_steps["mid_wf"].status is StepStatus.COMPLETED
        finally:
            if cp._sync_conn:
                cp._sync_conn.close()

    @pytest.mark.parametrize("error_handling", ["raise", "continue"])
    def test_resume_does_not_mask_failed_child(self, tmp_path, error_handling):
        parent, counters = build_failing_child_graph()
        cp = SqliteCheckpointer(str(tmp_path / "test.db"), durability="sync")
        cp._sync_db()
        try:
            runner = SyncRunner(checkpointer=cp)
            if error_handling == "raise":
                with pytest.raises(ValueError, match="child exploded"):
                    runner.run(parent, {"x": 5}, workflow_id="wf", error_handling=error_handling)
            else:
                first = runner.run(parent, {"x": 5}, workflow_id="wf", error_handling=error_handling)
                assert first.status is RunStatus.FAILED

            child_run = cp.get_run("wf/child_wf")
            assert child_run is not None
            assert child_run.status is WorkflowStatus.FAILED
            assert counters["boom"] == 1

            if error_handling == "raise":
                with pytest.raises(ValueError, match="child exploded"):
                    runner.run(parent, workflow_id="wf", error_handling=error_handling)
            else:
                result = runner.run(parent, workflow_id="wf", error_handling=error_handling)
                assert result.status is RunStatus.FAILED
                assert isinstance(result.error, ValueError)
                assert "doubled" not in result.values
                assert "final" not in result.values

            assert counters["boom"] == 2
            child_run = cp.get_run("wf/child_wf")
            assert child_run is not None
            assert child_run.status is WorkflowStatus.FAILED
        finally:
            if cp._sync_conn:
                cp._sync_conn.close()
