"""Tests for checkpointer types and CheckpointPolicy validation."""

import pytest

from hypergraph.checkpointers import CheckpointPolicy, StepRecord, StepStatus, Workflow, WorkflowStatus


class TestCheckpointPolicy:
    def test_default_policy(self):
        policy = CheckpointPolicy()
        assert policy.durability == "async"
        assert policy.retention == "full"
        assert policy.window is None
        assert policy.ttl is None

    def test_sync_full(self):
        policy = CheckpointPolicy(durability="sync", retention="full")
        assert policy.durability == "sync"

    def test_exit_latest(self):
        policy = CheckpointPolicy(durability="exit", retention="latest")
        assert policy.durability == "exit"

    def test_exit_full_raises(self):
        with pytest.raises(ValueError, match='durability="exit" requires retention="latest"'):
            CheckpointPolicy(durability="exit", retention="full")

    def test_exit_windowed_raises(self):
        with pytest.raises(ValueError, match='durability="exit" requires retention="latest"'):
            CheckpointPolicy(durability="exit", retention="windowed", window=10)

    def test_windowed_requires_window(self):
        with pytest.raises(ValueError, match="requires window parameter"):
            CheckpointPolicy(retention="windowed")

    def test_window_only_valid_with_windowed(self):
        with pytest.raises(ValueError, match="window parameter only valid"):
            CheckpointPolicy(retention="full", window=10)

    def test_windowed_with_window(self):
        policy = CheckpointPolicy(retention="windowed", window=50)
        assert policy.window == 50


class TestStepRecord:
    def test_frozen(self):
        record = StepRecord(
            workflow_id="wf-1",
            superstep=0,
            node_name="embed",
            index=0,
            status=StepStatus.COMPLETED,
            input_versions={"x": 1},
        )
        with pytest.raises(AttributeError):
            record.node_name = "other"  # type: ignore[misc]

    def test_to_dict(self):
        record = StepRecord(
            workflow_id="wf-1",
            superstep=0,
            node_name="embed",
            index=0,
            status=StepStatus.COMPLETED,
            input_versions={"x": 1},
            values={"embedding": [1, 2, 3]},
            duration_ms=42.5,
            cached=True,
        )
        d = record.to_dict()
        assert d["workflow_id"] == "wf-1"
        assert d["status"] == "completed"
        assert d["values"] == {"embedding": [1, 2, 3]}
        assert d["cached"] is True

    def test_defaults(self):
        record = StepRecord(
            workflow_id="wf-1",
            superstep=0,
            node_name="embed",
            index=0,
            status=StepStatus.COMPLETED,
            input_versions={},
        )
        assert record.values is None
        assert record.duration_ms == 0.0
        assert record.cached is False
        assert record.decision is None
        assert record.error is None
        assert record.child_workflow_id is None


class TestWorkflow:
    def test_to_dict(self):
        wf = Workflow(id="wf-1", status=WorkflowStatus.ACTIVE, graph_name="my_graph")
        d = wf.to_dict()
        assert d["id"] == "wf-1"
        assert d["status"] == "active"
        assert d["graph_name"] == "my_graph"
