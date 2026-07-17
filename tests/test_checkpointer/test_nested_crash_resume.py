"""Crash-window recovery for nested GraphNodes (issue #235).

Witness (reproduced during the #187 investigation): a crash lands between a
child workflow committing terminal COMPLETED and the parent writing its
GraphNode StepRecord. On resume the parent must RESTORE the child's persisted
outputs and commit the missing parent step — not re-invoke the terminal child
(which raises ``WorkflowAlreadyCompletedError``) and never silently restore a
FAILED child as success.
"""

import pytest

from hypergraph import END, AsyncRunner, CompactedRetentionError, Graph, SyncRunner, interrupt, node, route
from hypergraph.checkpointers import CheckpointPolicy, SqliteCheckpointer, WorkflowStatus
from hypergraph.checkpointers.types import StepStatus
from hypergraph.events import EventProcessor
from hypergraph.events.types import NodeEndEvent, NodeStartEvent, RunStartEvent
from hypergraph.runners._shared.results import RunStatus
from tests._interrupt_questions import StringQuestion

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

    def __init__(
        self,
        path: str,
        targets: set[tuple[str, str]],
        *,
        policy: CheckpointPolicy | None = None,
    ):
        if policy is None:
            super().__init__(path, durability="sync")
        else:
            super().__init__(path, policy=policy)
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


def build_nested_graph(child_runner=None):
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
    parent = Graph(nodes=[prepare, child.as_node(name="child_wf", runner=child_runner), consume], name="parent")
    return parent, counters


def build_multi_turn_graph():
    """Cyclic chat shape: ask (interrupt) -> child_wf(respond) -> gate -> ask.

    Each turn ends in a pause; every resume that supplies a new answer must
    RE-execute the nested child under a fresh suffixed child id — never treat
    the previous turn's terminal child as a crash window to restore from.
    """
    calls: list[str] = []

    @interrupt(answer_name="answer")
    def ask(prompt: str = "next?") -> StringQuestion:
        return StringQuestion(prompt=prompt)

    @node(output_name="reply")
    def respond(answer: str) -> str:
        calls.append(answer)
        return f"reply:{answer}"

    child = Graph(nodes=[respond], name="turn")

    @route(targets=["ask", END])
    def gate(reply: str) -> str:
        return END if reply == "reply:two" else "ask"

    parent = Graph(nodes=[ask, child.as_node(name="child_wf"), gate], name="chat", entrypoint="ask")
    return parent, calls


def retention_policy(retention: str) -> CheckpointPolicy:
    if retention == "windowed":
        return CheckpointPolicy(durability="sync", retention="windowed", window=1)
    return CheckpointPolicy(durability="sync", retention=retention)


def assert_compacted_retention_guidance(error: pytest.ExceptionInfo[BaseException]) -> None:
    """The rejection names the boundary and both supported escape hatches."""
    message = str(error.value)
    assert "windowed" in message
    assert "compacted" in message
    assert "retention='full'" in message
    assert "retention='latest'" in message
    assert "fork" in message.lower()
    assert "#277" in message


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


def build_zero_step_failing_child_graph():
    """Parent whose child fails before persisting any completed step."""
    counters = {"boom": 0}

    @node(output_name="prepared")
    def prepare(x: int) -> int:
        return x + 100

    @node(output_name="doubled")
    def boom(prepared: int) -> int:
        counters["boom"] += 1
        raise ValueError("first child step exploded")

    child = Graph(nodes=[boom], name="child")
    parent = Graph(nodes=[prepare, child.as_node(name="child_wf")], name="parent")
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

    async def test_resume_restores_namespaced_child_boundary(self, tmp_path):
        """Restored outputs project through a namespaced GraphNode boundary."""
        counters = {"double": 0}

        @node(output_name="prepared")
        def prepare(x: int) -> int:
            return x + 100

        @node(output_name="doubled")
        def double(prepared: int) -> int:
            counters["double"] += 1
            return prepared * 2

        @node(output_name="final")
        def consume(doubled: int) -> int:
            return doubled + 1

        child = Graph(nodes=[double], name="child")
        child_node = child.as_node(name="child_wf", namespaced=True).expose("prepared")
        parent = Graph(
            nodes=[
                prepare,
                child_node,
                consume.with_inputs(doubled="child_wf.doubled"),
            ],
            name="parent",
        )

        cp = CrashingStepCheckpointer(str(tmp_path / "test.db"), {("wf", "child_wf")})
        try:
            runner = AsyncRunner(checkpointer=cp)
            with pytest.raises(RuntimeError, match=CRASH_MESSAGE):
                await runner.run(parent, {"x": 5}, workflow_id="wf")
            child_run = await cp.get_run_async("wf/child_wf")
            assert child_run is not None
            assert child_run.status is WorkflowStatus.COMPLETED
            assert counters["double"] == 1

            cp.armed = False
            result = await runner.run(parent, workflow_id="wf")

            assert result.values["child_wf.doubled"] == 210
            assert result.values["final"] == 211
            assert counters["double"] == 1
            steps = {step.node_name: step for step in await cp.get_steps("wf")}
            assert steps["child_wf"].status is StepStatus.COMPLETED
            assert steps["child_wf"].values == {"child_wf.doubled": 210}
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

    @pytest.mark.parametrize("error_handling", ["raise", "continue"])
    async def test_resume_replays_failed_child_with_no_completed_steps(self, tmp_path, error_handling):
        """#270: child seeds survive resume when its first step failed."""
        parent, counters = build_zero_step_failing_child_graph()
        cp = SqliteCheckpointer(str(tmp_path / "test.db"), durability="sync")
        try:
            runner = AsyncRunner(checkpointer=cp)
            if error_handling == "raise":
                with pytest.raises(ValueError, match="first child step exploded"):
                    await runner.run(parent, {"x": 5}, workflow_id="wf", error_handling=error_handling)
            else:
                first = await runner.run(parent, {"x": 5}, workflow_id="wf", error_handling=error_handling)
                assert first.status is RunStatus.FAILED
                assert isinstance(first.error, ValueError)

            child_steps = await cp.get_steps("wf/child_wf")
            assert not any(step.status is StepStatus.COMPLETED for step in child_steps)

            if error_handling == "raise":
                with pytest.raises(ValueError, match="first child step exploded"):
                    await runner.run(parent, workflow_id="wf", error_handling=error_handling)
            else:
                resumed = await runner.run(parent, workflow_id="wf", error_handling=error_handling)
                assert resumed.status is RunStatus.FAILED
                assert isinstance(resumed.error, ValueError)
                assert str(resumed.error) == "first child step exploded"

            assert counters["boom"] == 2
        finally:
            await cp.close()

    async def test_paused_child_persists_runtime_answer_before_parent_step(self, tmp_path):
        """#278: a terminal child durably folds its consumed interrupt answer."""

        @node(output_name="draft")
        def prepare(query: str) -> str:
            return f"Draft for: {query}"

        @interrupt(answer_name="decision")
        def approval(draft: str) -> StringQuestion:
            return StringQuestion(prompt="Approve?", evidence=(draft,))

        @node(output_name="result")
        def finalize(decision: str) -> str:
            return f"Final: {decision}"

        child = Graph([approval], name="review")
        parent = Graph([prepare, child.as_node(name="child_wf"), finalize], name="parent")
        cp = CrashingStepCheckpointer(str(tmp_path / "test.db"), {("wf", "child_wf")})
        cp.armed = False
        try:
            runner = AsyncRunner(checkpointer=cp)
            paused = await runner.run(parent, {"query": "hello"}, workflow_id="wf")
            assert paused.status is RunStatus.PAUSED
            assert await cp.get_state("wf/child_wf") == {}

            cp.armed = True
            with pytest.raises(RuntimeError, match=CRASH_MESSAGE):
                await runner.run(parent, {"decision": "approved"}, workflow_id="wf")

            child_run = await cp.get_run_async("wf/child_wf")
            assert child_run is not None
            assert child_run.status is WorkflowStatus.COMPLETED
            assert await cp.get_state("wf/child_wf") == {"decision": "approved"}
            child_steps = await cp.get_steps("wf/child_wf")
            assert any(
                step.node_name == "approval" and step.status is StepStatus.COMPLETED and step.values == {"decision": "approved"}
                for step in child_steps
            )

            cp.armed = False
            restored = await runner.run(parent, workflow_id="wf")
            assert restored.values == {
                "draft": "Draft for: hello",
                "decision": "approved",
                "result": "Final: approved",
            }
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

    @pytest.mark.parametrize("error_handling", ["raise", "continue"])
    def test_resume_replays_failed_child_with_no_completed_steps(self, tmp_path, error_handling):
        """#270: child seeds survive resume when its first step failed."""
        parent, counters = build_zero_step_failing_child_graph()
        cp = SqliteCheckpointer(str(tmp_path / "test.db"), durability="sync")
        cp._sync_db()
        try:
            runner = SyncRunner(checkpointer=cp)
            if error_handling == "raise":
                with pytest.raises(ValueError, match="first child step exploded"):
                    runner.run(parent, {"x": 5}, workflow_id="wf", error_handling=error_handling)
            else:
                first = runner.run(parent, {"x": 5}, workflow_id="wf", error_handling=error_handling)
                assert first.status is RunStatus.FAILED
                assert isinstance(first.error, ValueError)

            child_steps = cp.steps("wf/child_wf")
            assert not any(step.status is StepStatus.COMPLETED for step in child_steps)

            if error_handling == "raise":
                with pytest.raises(ValueError, match="first child step exploded"):
                    runner.run(parent, workflow_id="wf", error_handling=error_handling)
            else:
                resumed = runner.run(parent, workflow_id="wf", error_handling=error_handling)
                assert resumed.status is RunStatus.FAILED
                assert isinstance(resumed.error, ValueError)
                assert str(resumed.error) == "first child step exploded"

            assert counters["boom"] == 2
        finally:
            if cp._sync_conn:
                cp._sync_conn.close()


class TestCompactedRetentionNestedRecovery:
    """F1 re-review: compacted parent history is not completion provenance."""

    @pytest.mark.parametrize("retention", ["full", "latest"])
    async def test_async_multi_turn_reexecution_survives_non_windowed_retention(self, tmp_path, retention):
        parent, calls = build_multi_turn_graph()
        cp = SqliteCheckpointer(str(tmp_path / "test.db"), policy=retention_policy(retention))
        try:
            runner = AsyncRunner(checkpointer=cp)
            first = await runner.run(parent, {}, workflow_id="wfm")
            assert first.status is RunStatus.PAUSED

            second = await runner.run(parent, {"answer": "one"}, workflow_id="wfm")
            assert second.status is RunStatus.PAUSED
            assert calls == ["one"]

            third = await runner.run(parent, {"answer": "two"}, workflow_id="wfm")
            assert third.status is RunStatus.COMPLETED
            assert third.values["reply"] == "reply:two"
            # The child re-executed with the new answer — no stale restore.
            assert calls == ["one", "two"]

            # The re-execution ran under a fresh suffixed child id.
            turn2_run = await cp.get_run_async("wfm/child_wf/1")
            assert turn2_run is not None
            assert turn2_run.status is WorkflowStatus.COMPLETED
            child_steps = [step for step in await cp.get_steps("wfm") if step.node_name == "child_wf" and step.status is StepStatus.COMPLETED]
            assert child_steps
            assert child_steps[-1].child_run_id == "wfm/child_wf/1"
        finally:
            await cp.close()

    async def test_async_windowed_multi_turn_rejects_ambiguous_resume(self, tmp_path):
        parent, calls = build_multi_turn_graph()
        cp = SqliteCheckpointer(str(tmp_path / "test.db"), policy=retention_policy("windowed"))
        try:
            runner = AsyncRunner(checkpointer=cp)
            first = await runner.run(parent, {}, workflow_id="wfm")
            assert first.status is RunStatus.PAUSED

            second = await runner.run(parent, {"answer": "one"}, workflow_id="wfm")
            assert second.status is RunStatus.PAUSED
            assert calls == ["one"]

            with pytest.raises(CompactedRetentionError) as error:
                await runner.run(parent, {"answer": "two"}, workflow_id="wfm")

            assert_compacted_retention_guidance(error)
            assert calls == ["one"]
        finally:
            await cp.close()

    def test_sync_windowed_pruned_completion_rejects_ambiguous_resume(self, tmp_path):
        """Sync parity for the compacted-history rejection.

        SyncRunner does not support interrupts (validated capability boundary:
        IncompatibleRunnerError says "Use AsyncRunner instead"), so the
        multi-turn interrupt repro is structurally impossible on sync. This
        variant changes the child's input across a FAILED-parent resume via an
        impure upstream node: windowed retention prunes the prior completion,
        and a stale restore would feed the downstream node outdated output.
        """
        child_inputs: list[int] = []
        run_counter = {"n": 0}
        flaky_armed = [True]

        @node(output_name="prepared")
        def prepare(x: int = 5) -> int:
            run_counter["n"] += 1
            return x + 100 * run_counter["n"]

        @node(output_name="doubled")
        def double(prepared: int) -> int:
            child_inputs.append(prepared)
            return prepared * 2

        @node(output_name="final")
        def flaky(doubled: int) -> int:
            if flaky_armed[0]:
                raise RuntimeError("flaky failure")
            return doubled + 1

        child = Graph(nodes=[double], name="child")
        parent = Graph(nodes=[prepare, child.as_node(name="child_wf"), flaky], name="parent")

        cp = SqliteCheckpointer(str(tmp_path / "test.db"), policy=retention_policy("windowed"))
        cp._sync_db()
        try:
            runner = SyncRunner(checkpointer=cp)
            with pytest.raises(RuntimeError, match="flaky failure"):
                runner.run(parent, {}, workflow_id="wfw")
            assert child_inputs == [105]
            assert cp.get_run("wfw/child_wf").status is WorkflowStatus.COMPLETED
            # windowed retention pruned the parent's completed rows.
            assert "child_wf" not in {step.node_name for step in cp.steps("wfw")}

            flaky_armed[0] = False
            with pytest.raises(CompactedRetentionError) as error:
                runner.run(parent, workflow_id="wfw")

            assert_compacted_retention_guidance(error)
            assert child_inputs == [105]
        finally:
            if cp._sync_conn:
                cp._sync_conn.close()

    def test_windowed_same_named_shared_value_is_not_completion_evidence(self, tmp_path):
        """A carrier value from another producer cannot prove child completion."""
        calls: list[int] = []

        @node(output_name=("prepared", "result"))
        def prepare(x: int = 2) -> tuple[int, int]:
            return x, x * 10

        @node(output_name="ready")
        def order_after_prepare(prepared: int) -> int:
            return prepared

        @node(output_name="result")
        def child_work(ready: int, result: int) -> int:
            calls.append(ready)
            return ready * 100 + result

        child = Graph(nodes=[child_work], name="child", shared=["result"])
        parent = Graph(
            nodes=[prepare, order_after_prepare, child.as_node(name="child_wf")],
            name="parent",
            shared=["result"],
        )

        cp = CrashingStepCheckpointer(
            str(tmp_path / "test.db"),
            {("wfe", "child_wf")},
            policy=retention_policy("windowed"),
        )
        cp._sync_db()
        try:
            runner = SyncRunner(checkpointer=cp)
            with pytest.raises(RuntimeError, match=CRASH_MESSAGE):
                runner.run(parent, {"result": 0}, workflow_id="wfe")

            assert calls == [2]
            internal_steps = cp.steps("wfe", show_internal=True)
            baseline = next(step for step in internal_steps if step.node_type == "RetentionBaseline")
            assert baseline.values is not None
            assert baseline.values["result"] == 20
            assert cp.get_run("wfe/child_wf").status is WorkflowStatus.COMPLETED

            cp.armed = False
            with pytest.raises(CompactedRetentionError) as error:
                runner.run(parent, workflow_id="wfe")

            assert_compacted_retention_guidance(error)
            assert calls == [2]
        finally:
            if cp._sync_conn:
                cp._sync_conn.close()


class TestDelegatedRunnerCrashResume:
    """F2 (review): a delegated runner owns the child's persistence boundary.

    The child's terminal COMPLETED state lives in the runner_override's
    checkpointer, not the parent's. Crash-window recovery must read child
    status and state from the effective runner's persistence.
    """

    async def test_async_delegated_child_checkpointer_crash_resume(self, tmp_path):
        child_cp = SqliteCheckpointer(str(tmp_path / "child.db"), durability="sync")
        parent, counters = build_nested_graph(child_runner=AsyncRunner(checkpointer=child_cp))
        parent_cp = CrashingStepCheckpointer(str(tmp_path / "parent.db"), {("wf", "child_wf")})
        try:
            runner = AsyncRunner(checkpointer=parent_cp)
            with pytest.raises(RuntimeError, match=CRASH_MESSAGE):
                await runner.run(parent, {"x": 5}, workflow_id="wf")

            # The witness lives across two stores: the child completed in the
            # delegated checkpointer; the parent has no child row at all.
            child_run = await child_cp.get_run_async("wf/child_wf")
            assert child_run is not None
            assert child_run.status is WorkflowStatus.COMPLETED
            assert await parent_cp.get_run_async("wf/child_wf") is None
            assert "child_wf" not in {step.node_name for step in await parent_cp.get_steps("wf")}
            assert counters == {"prepare": 1, "double": 1, "consume": 0}

            parent_cp.armed = False
            result = await runner.run(parent, workflow_id="wf")

            assert result.values["doubled"] == 210
            assert result.values["final"] == 211
            assert counters["double"] == 1
            assert counters["consume"] == 1

            steps = {step.node_name: step for step in await parent_cp.get_steps("wf")}
            child_step = steps["child_wf"]
            assert child_step.status is StepStatus.COMPLETED
            assert child_step.values == {"doubled": 210}
            assert child_step.child_run_id == "wf/child_wf"
        finally:
            await parent_cp.close()
            await child_cp.close()

    def test_sync_delegated_child_checkpointer_crash_resume(self, tmp_path):
        child_cp = SqliteCheckpointer(str(tmp_path / "child.db"), durability="sync")
        child_cp._sync_db()
        parent, counters = build_nested_graph(child_runner=SyncRunner(checkpointer=child_cp))
        parent_cp = CrashingStepCheckpointer(str(tmp_path / "parent.db"), {("wf", "child_wf")})
        parent_cp._sync_db()
        try:
            runner = SyncRunner(checkpointer=parent_cp)
            with pytest.raises(RuntimeError, match=CRASH_MESSAGE):
                runner.run(parent, {"x": 5}, workflow_id="wf")

            child_run = child_cp.get_run("wf/child_wf")
            assert child_run is not None
            assert child_run.status is WorkflowStatus.COMPLETED
            assert parent_cp.get_run("wf/child_wf") is None
            assert counters == {"prepare": 1, "double": 1, "consume": 0}

            parent_cp.armed = False
            result = runner.run(parent, workflow_id="wf")

            assert result.values["doubled"] == 210
            assert result.values["final"] == 211
            assert counters["double"] == 1
            assert counters["consume"] == 1

            steps = {step.node_name: step for step in parent_cp.steps("wf")}
            child_step = steps["child_wf"]
            assert child_step.status is StepStatus.COMPLETED
            assert child_step.values == {"doubled": 210}
            assert child_step.child_run_id == "wf/child_wf"
        finally:
            if child_cp._sync_conn:
                child_cp._sync_conn.close()
            if parent_cp._sync_conn:
                parent_cp._sync_conn.close()


class TestCrashRestoreTupleOutputs:
    """F3 (review): restored outputs must mirror real execution for tuples.

    JSON persistence turns tuples into lists; the restore path (like ordinary
    checkpoint resume) must coerce annotated tuple outputs back.
    """

    async def test_restored_tuple_output_reaches_downstream_as_tuple(self, tmp_path):
        received: list[object] = []

        @node(output_name="prepared")
        def prepare(x: int) -> int:
            return x + 100

        @node(output_name="pair")
        def make_pair(prepared: int) -> tuple[int, int]:
            return (prepared, prepared + 1)

        @node(output_name="total")
        def consume(pair: tuple[int, int]) -> int:
            received.append(pair)
            return pair[0] + pair[1]

        child = Graph(nodes=[make_pair], name="child")
        parent = Graph(nodes=[prepare, child.as_node(name="child_wf"), consume], name="parent")

        cp = CrashingStepCheckpointer(str(tmp_path / "test.db"), {("wf", "child_wf")})
        try:
            runner = AsyncRunner(checkpointer=cp)
            with pytest.raises(RuntimeError, match=CRASH_MESSAGE):
                await runner.run(parent, {"x": 5}, workflow_id="wf")
            cp.armed = False

            result = await runner.run(parent, workflow_id="wf")

            assert result.values["total"] == 211
            assert received == [(105, 106)]
            assert isinstance(received[0], tuple)
            assert result.values["pair"] == (105, 106)
            assert isinstance(result.values["pair"], tuple)
        finally:
            await cp.close()
