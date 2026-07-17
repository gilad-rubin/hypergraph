"""Contract tests for policy fingerprints, persisted manifest, resume validation (#232).

Assertion map (ticket red-green items):
    1   changed policy rejects same-workflow resume     TestSameWorkflowResumeValidation
    2   fork adopts a new policy with a fresh series    test_fork_adopts_new_policy_with_fresh_series
    3   policy is not cache identity                    test_cache_hits_survive_policy_change
    4   fingerprint stability                           TestPolicyFingerprint (tests/test_retry_policy.py)

Plus the manifest unit surface: per-node collection, normalized fields,
field-level diffs, config round-trip, and legacy-config tolerance.

Repo flaky rule: no wall-clock assertions. Sleeps are intercepted via the
coordinator's _sleep_sync/_sleep_async seams; time is fixed via _utcnow.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from hypergraph import (
    AsyncRunner,
    Graph,
    InMemoryCache,
    RetryPolicy,
    RetryPolicyChangedError,
    SyncRunner,
    node,
)
from hypergraph.checkpointers import AttemptStatus, SqliteCheckpointer, WorkflowStatus
from hypergraph.runners._shared import attempts as attempts_module
from hypergraph.runners._shared.lineage import ResumeAction, resolve_existing_run
from hypergraph.runners._shared.policy_manifest import (
    RETRY_POLICY_CONFIG_KEY,
    NodePolicyRecord,
    RetryPolicyManifest,
    diff_policy_manifests,
)

aiosqlite = pytest.importorskip("aiosqlite")


# === Helpers ===


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

    def factory(name: str = "retry.db") -> SqliteCheckpointer:
        cp = SqliteCheckpointer(str(tmp_path / name))
        created.append(cp)
        return cp

    yield factory
    for cp in created:
        await cp.close()


@pytest.fixture
def recorded_sleeps(monkeypatch):
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def fake_sleep_async(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(attempts_module, "_sleep_sync", fake_sleep)
    monkeypatch.setattr(attempts_module, "_sleep_async", fake_sleep_async)
    return sleeps


class _SimulatedProcessDeath(BaseException):
    pass


def _crash_mid_backoff(monkeypatch) -> None:
    """First backoff sleep kills the 'process', stranding an OPEN series."""

    def dying_sleep(seconds: float) -> None:
        raise _SimulatedProcessDeath

    async def dying_sleep_async(seconds: float) -> None:
        raise _SimulatedProcessDeath

    monkeypatch.setattr(attempts_module, "_sleep_sync", dying_sleep)
    monkeypatch.setattr(attempts_module, "_sleep_async", dying_sleep_async)


def _flaky_graph(calls: list[int], **policy_overrides) -> Graph:
    """One retrying node. x has a default so a bare same-workflow resume works."""

    @node(output_name="fetched", retry=_policy(**policy_overrides))
    def flaky(x: int = 1) -> int:
        calls.append(x)
        if len(calls) == 1:
            raise ConnectionError("transient")
        return x * 10

    return Graph([flaky])


# === Red-green item 1: changed policy rejects same-workflow resume ===


class TestSameWorkflowResumeValidation:
    async def _crash_open_series(self, family, cp, calls, monkeypatch):
        frozen_now = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(attempts_module, "_utcnow", lambda: frozen_now)
        _crash_mid_backoff(monkeypatch)
        graph = _flaky_graph(calls, max_attempts=3, initial_delay=3600.0, jitter="full")
        runner = _make_runner(family, checkpointer=cp)
        with pytest.raises(_SimulatedProcessDeath):
            await _run(runner, graph, workflow_id="wf-policy")
        assert calls == [1]
        series = await cp.get_open_attempt_series("wf-policy", "flaky")
        assert series is not None, "the series must survive the crash open"
        return series

    async def test_changed_max_attempts_rejects_resume_before_user_code(self, family, make_sqlite, monkeypatch):
        cp = make_sqlite()
        calls: list[int] = []
        series = await self._crash_open_series(family, cp, calls, monkeypatch)

        # "New process": same graph shape, but the budget grew 3 → 5.
        changed = _flaky_graph(calls, max_attempts=5, initial_delay=3600.0, jitter="full")
        resumed_sleeps: list[float] = []

        def recording_sleep(seconds: float) -> None:
            resumed_sleeps.append(seconds)

        async def recording_sleep_async(seconds: float) -> None:
            resumed_sleeps.append(seconds)

        monkeypatch.setattr(attempts_module, "_sleep_sync", recording_sleep)
        monkeypatch.setattr(attempts_module, "_sleep_async", recording_sleep_async)

        resumed_runner = _make_runner(family, checkpointer=make_sqlite())
        with pytest.raises(RetryPolicyChangedError) as exc_info:
            await _run(resumed_runner, changed, workflow_id="wf-policy")

        error = exc_info.value
        assert error.code == "HG_RETRY_POLICY_CHANGED"
        assert error.workflow_id == "wf-policy"
        assert [(c.node_name, c.field, c.stored, c.current) for c in error.changes] == [("flaky", "max_attempts", 3, 5)]
        assert "max_attempts" in str(error)
        assert "HG_RETRY_POLICY_CHANGED" in str(error)

        # The sentinel: rejection precedes user code AND the persisted world.
        assert calls == [1], "no user code may run on a rejected resume"
        assert resumed_sleeps == [], "rejection precedes the persisted backoff wait"
        run = await cp.get_run_async("wf-policy")
        stored = RetryPolicyManifest.from_config(run.config)
        assert stored is not None
        assert stored.entries[0].max_attempts == 3, "create_run must not overwrite the stored manifest"
        still_open = await cp.get_open_attempt_series("wf-policy", "flaky")
        assert still_open is not None and still_open.id == series.id
        records = await cp.get_attempt_records(series.id)
        assert [r.status for r in records] == [AttemptStatus.FAILED], "no new reservation was consumed"

    async def test_identical_policy_resumes_and_completes(self, family, make_sqlite, monkeypatch):
        cp = make_sqlite()
        calls: list[int] = []
        series = await self._crash_open_series(family, cp, calls, monkeypatch)

        same = _flaky_graph(calls, max_attempts=3, initial_delay=3600.0, jitter="full")
        records = await cp.get_attempt_records(series.id)
        resumed_now = records[0].retry_not_before
        assert resumed_now is not None
        monkeypatch.setattr(attempts_module, "_utcnow", lambda: resumed_now)
        resumed_sleeps: list[float] = []
        monkeypatch.setattr(attempts_module, "_sleep_sync", lambda seconds: resumed_sleeps.append(seconds))

        async def recording_sleep_async(seconds: float) -> None:
            resumed_sleeps.append(seconds)

        monkeypatch.setattr(attempts_module, "_sleep_async", recording_sleep_async)

        resumed_runner = _make_runner(family, checkpointer=make_sqlite())
        result = await _run(resumed_runner, same, workflow_id="wf-policy")
        assert result["fetched"] == 10
        assert calls == [1, 1], "an unchanged policy continues the same budget"


# === Red-green item 2: fork adopts the new policy with a fresh series ===


async def test_fork_adopts_new_policy_with_fresh_series(family, make_sqlite, monkeypatch):
    cp = make_sqlite()
    calls: list[int] = []
    frozen_now = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(attempts_module, "_utcnow", lambda: frozen_now)
    _crash_mid_backoff(monkeypatch)

    graph = _flaky_graph(calls, max_attempts=3, initial_delay=3600.0, jitter="full")
    runner = _make_runner(family, checkpointer=cp)
    with pytest.raises(_SimulatedProcessDeath):
        await _run(runner, graph, workflow_id="wf-fork")
    original_series = await cp.get_open_attempt_series("wf-fork", "flaky")
    assert original_series is not None

    # Fork is a deliberate new lineage: the new policy is free to differ.
    new_policy_graph = _flaky_graph(calls, max_attempts=5, initial_delay=3600.0, jitter="full")
    forked_runner = _make_runner(family, checkpointer=make_sqlite())
    forked = await _run(forked_runner, new_policy_graph, fork_from="wf-fork")

    assert forked["fetched"] == 10
    forked_wf = forked.workflow_id
    assert forked_wf is not None and forked_wf.startswith("wf-fork-fork-")

    # Fresh series under the fork, owned by the NEW policy.
    steps = [s for s in await cp.get_steps(forked_wf) if s.node_name == "flaky"]
    assert len(steps) == 1 and steps[0].attempt_series_id is not None
    forked_series = await cp.get_attempt_series(steps[0].attempt_series_id)
    assert forked_series is not None
    assert forked_series.id != original_series.id
    assert forked_series.max_attempts == 5
    new_fingerprint = RetryPolicy(max_attempts=5, retry_on=(ConnectionError,), initial_delay=3600.0, jitter="full").fingerprint
    assert forked_series.policy_fingerprint == new_fingerprint

    # The fork's run config records the policy it actually ran with.
    forked_run = await cp.get_run_async(forked_wf)
    manifest = RetryPolicyManifest.from_config(forked_run.config)
    assert manifest is not None and manifest.entries[0].max_attempts == 5

    # The abandoned lineage keeps its own open series and stored manifest.
    untouched = await cp.get_open_attempt_series("wf-fork", "flaky")
    assert untouched is not None and untouched.id == original_series.id
    original_run = await cp.get_run_async("wf-fork")
    original_manifest = RetryPolicyManifest.from_config(original_run.config)
    assert original_manifest is not None and original_manifest.entries[0].max_attempts == 3


# === Red-green item 3: policy stays out of successful-output cache identity ===


class _CountingCache(InMemoryCache):
    def __init__(self) -> None:
        super().__init__()
        self.set_calls = 0

    def set(self, key, value) -> None:
        self.set_calls += 1
        super().set(key, value)


async def test_cache_hits_survive_policy_change(family, recorded_sleeps):
    cache = _CountingCache()
    calls: list[int] = []

    def build_graph(policy: RetryPolicy) -> Graph:
        @node(output_name="fetched", cache=True, retry=policy)
        def flaky(x: int) -> int:
            calls.append(x)
            if len(calls) == 1:
                raise ConnectionError("transient")
            return x * 10

        return Graph([flaky])

    first_runner = _make_runner(family, cache=cache)
    first = await _run(first_runner, build_graph(_policy(max_attempts=3)), {"x": 1})
    assert first["fetched"] == 10
    assert calls == [1, 1]
    assert cache.set_calls == 1

    # Change every budget knob; the cached success must still be found.
    changed_policy = _policy(max_attempts=7, initial_delay=0.5, backoff_multiplier=3.0, max_delay=9.0, jitter="full", retry_window=120.0)
    second_runner = _make_runner(family, cache=cache)
    second = await _run(second_runner, build_graph(changed_policy), {"x": 1})
    assert second["fetched"] == 10
    assert calls == [1, 1], "a cache hit invokes nothing despite the policy change"
    assert cache.set_calls == 1, "no second cache write"
    assert second.log is not None
    (step,) = [s for s in second.log.steps if s.node_name == "flaky"]
    assert step.cached is True


# === Manifest surface: collection, normalization, diffs, round-trip ===


def _graph_with_policies() -> Graph:
    @node(output_name="fetched", retry=RetryPolicy(max_attempts=3, retry_on=(TimeoutError, ConnectionError), retry_window=45.0))
    def fetch(x: int) -> int:
        return x

    @node(output_name="plain")
    def plain(fetched: int) -> int:
        return fetched

    @node(output_name="pushed", timeout=30)
    async def push(plain: int) -> int:
        return plain

    return Graph([fetch, plain, push])


class TestRetryPolicyManifest:
    def test_from_graph_collects_only_policy_bearing_nodes(self):
        manifest = RetryPolicyManifest.from_graph(_graph_with_policies())
        assert [entry.node_name for entry in manifest.entries] == ["fetch", "push"]

        fetch_entry, push_entry = manifest.entries
        assert fetch_entry.max_attempts == 3
        assert fetch_entry.retry_on == ("ConnectionError", "TimeoutError"), "retry_on is normalized sorted"
        assert fetch_entry.retry_window == 45.0
        assert fetch_entry.timeout is None
        assert fetch_entry.policy_fingerprint == RetryPolicy(max_attempts=3, retry_on=(ConnectionError, TimeoutError), retry_window=45.0).fingerprint

        assert push_entry.max_attempts is None
        assert push_entry.retry_on == ()
        assert push_entry.policy_fingerprint is None
        assert push_entry.timeout == 30.0

    def test_config_round_trip(self):
        manifest = RetryPolicyManifest.from_graph(_graph_with_policies())
        config = {RETRY_POLICY_CONFIG_KEY: manifest.to_config_value()}
        assert RetryPolicyManifest.from_config(config) == manifest

    def test_missing_key_is_a_legacy_config(self):
        assert RetryPolicyManifest.from_config(None) is None
        assert RetryPolicyManifest.from_config({"graph_struct_hash": "abc"}) is None

    def test_malformed_manifest_is_ignored(self):
        assert RetryPolicyManifest.from_config({RETRY_POLICY_CONFIG_KEY: "garbage"}) is None
        assert RetryPolicyManifest.from_config({RETRY_POLICY_CONFIG_KEY: [{"unexpected": 1}]}) is None

    def test_empty_manifest_round_trips_as_present(self):
        empty = RetryPolicyManifest.from_graph(Graph([]))
        assert empty.entries == ()
        config = {RETRY_POLICY_CONFIG_KEY: empty.to_config_value()}
        assert RetryPolicyManifest.from_config(config) == empty

    def test_diff_reports_field_level_changes(self):
        stored = RetryPolicyManifest.from_graph(_flaky_graph([], max_attempts=3))
        current = RetryPolicyManifest.from_graph(_flaky_graph([], max_attempts=5, initial_delay=0.5))
        changes = diff_policy_manifests(stored, current)
        assert [(c.node_name, c.field, c.stored, c.current) for c in changes] == [
            ("flaky", "max_attempts", 3, 5),
            ("flaky", "initial_delay", 0.001, 0.5),
        ]

    def test_diff_reports_added_and_removed_policies(self):
        def build(with_policy: bool) -> Graph:
            kwargs = {"retry": _policy()} if with_policy else {}

            @node(output_name="fetched", **kwargs)
            def fetch(x: int) -> int:
                return x

            return Graph([fetch])

        added = diff_policy_manifests(RetryPolicyManifest.from_graph(build(False)), RetryPolicyManifest.from_graph(build(True)))
        assert ("fetch", "max_attempts", None, 3) in [(c.node_name, c.field, c.stored, c.current) for c in added]

        removed = diff_policy_manifests(RetryPolicyManifest.from_graph(build(True)), RetryPolicyManifest.from_graph(build(False)))
        assert ("fetch", "max_attempts", 3, None) in [(c.node_name, c.field, c.stored, c.current) for c in removed]

    def test_identical_manifests_have_no_diff(self):
        a = RetryPolicyManifest.from_graph(_graph_with_policies())
        b = RetryPolicyManifest.from_graph(_graph_with_policies())
        assert diff_policy_manifests(a, b) == ()

    def test_equivalent_retry_on_orders_produce_identical_entries(self):
        def build(first, second) -> Graph:
            @node(output_name="fetched", retry=RetryPolicy(max_attempts=3, retry_on=(first, second)))
            def fetch(x: int) -> int:
                return x

            return Graph([fetch])

        a = RetryPolicyManifest.from_graph(build(ConnectionError, TimeoutError))
        b = RetryPolicyManifest.from_graph(build(TimeoutError, ConnectionError))
        assert a == b
        assert diff_policy_manifests(a, b) == ()


# === resolve_existing_run precedence (pure decision layer) ===


class TestResolveExistingRunPolicyPrecedence:
    def _stored_config(self, graph: Graph) -> dict:
        return {
            "graph_struct_hash": graph.structural_hash,
            RETRY_POLICY_CONFIG_KEY: RetryPolicyManifest.from_graph(graph).to_config_value(),
        }

    def _resolve(self, run, graph, *, override_workflow=False):
        from hypergraph.checkpointers.types import Run

        assert isinstance(run, Run)
        return resolve_existing_run(
            existing_run=run,
            checkpoint=None,
            override_workflow=override_workflow,
            workflow_id="workflow",
            graph_hash=graph.structural_hash,
            graph=graph,
            resume_values={},
        )

    def test_policy_change_rejects_resume(self):
        from hypergraph.checkpointers.types import Run

        stored_graph = _flaky_graph([], max_attempts=3)
        run = Run(id="workflow", status=WorkflowStatus.ACTIVE, config=self._stored_config(stored_graph))
        with pytest.raises(RetryPolicyChangedError):
            self._resolve(run, _flaky_graph([], max_attempts=5))

    def test_override_workflow_forks_without_policy_validation(self):
        from hypergraph.checkpointers.types import Run

        stored_graph = _flaky_graph([], max_attempts=3)
        run = Run(id="workflow", status=WorkflowStatus.ACTIVE, config=self._stored_config(stored_graph))
        action = self._resolve(run, _flaky_graph([], max_attempts=5), override_workflow=True)
        assert action is ResumeAction.FORK_EXISTING

    def test_legacy_config_without_manifest_skips_validation(self):
        from hypergraph.checkpointers.types import Run

        run = Run(id="workflow", status=WorkflowStatus.ACTIVE, config={"graph_struct_hash": _flaky_graph([]).structural_hash})
        action = self._resolve(run, _flaky_graph([], max_attempts=5))
        assert action is ResumeAction.RESUME_EXISTING

    def test_unchanged_policy_resumes(self):
        from hypergraph.checkpointers.types import Run

        stored_graph = _flaky_graph([], max_attempts=3)
        run = Run(id="workflow", status=WorkflowStatus.ACTIVE, config=self._stored_config(stored_graph))
        action = self._resolve(run, _flaky_graph([], max_attempts=3))
        assert action is ResumeAction.RESUME_EXISTING

    def test_graph_change_keeps_precedence_over_policy_change(self):
        from hypergraph.checkpointers.types import Run
        from hypergraph.exceptions import GraphChangedError

        stored_graph = _flaky_graph([], max_attempts=3)
        config = self._stored_config(stored_graph)
        config["graph_struct_hash"] = "different-structure"
        run = Run(id="workflow", status=WorkflowStatus.ACTIVE, config=config)
        with pytest.raises(GraphChangedError):
            self._resolve(run, _flaky_graph([], max_attempts=5))


# === Persistence: the manifest travels with run configuration ===


async def test_run_config_persists_typed_manifest(family, make_sqlite, recorded_sleeps):
    cp = make_sqlite()
    calls: list[int] = []
    graph = _flaky_graph(calls, max_attempts=3)
    runner = _make_runner(family, checkpointer=cp)

    result = await _run(runner, graph, workflow_id="wf-manifest")
    assert result["fetched"] == 10

    run = await cp.get_run_async("wf-manifest")
    manifest = RetryPolicyManifest.from_config(run.config)
    assert manifest is not None
    assert manifest == RetryPolicyManifest.from_graph(graph)
    (entry,) = manifest.entries
    assert isinstance(entry, NodePolicyRecord)
    assert entry.policy_fingerprint == _policy(max_attempts=3).fingerprint
    # Hash identities stay separate from policy identity in the same config.
    assert run.config["graph_struct_hash"] == graph.structural_hash


async def test_policy_free_graph_persists_empty_manifest(family, make_sqlite):
    cp = make_sqlite()

    @node(output_name="doubled")
    def double(x: int) -> int:
        return x * 2

    runner = _make_runner(family, checkpointer=cp)
    await _run(runner, Graph([double]), {"x": 1}, workflow_id="wf-empty")

    run = await cp.get_run_async("wf-empty")
    manifest = RetryPolicyManifest.from_config(run.config)
    assert manifest is not None, "an empty manifest is recorded, not omitted"
    assert manifest.entries == ()


async def test_adding_a_policy_to_same_workflow_is_rejected(family, make_sqlite, recorded_sleeps):
    cp = make_sqlite()

    def build(with_retry: bool) -> Graph:
        kwargs = {"retry": _policy()} if with_retry else {}

        @node(output_name="fetched", **kwargs)
        def fetch(x: int = 1) -> int:
            raise ValueError("boom")

        return Graph([fetch])

    runner = _make_runner(family, checkpointer=cp)
    # First run: recorded with an EMPTY manifest; it fails and stays resumable.
    with pytest.raises(ValueError, match="boom"):
        await _run(runner, build(False), workflow_id="wf-added")

    # Rerun with a retry declaration added: [] -> declared is a policy change.
    with pytest.raises(RetryPolicyChangedError) as exc_info:
        await _run(runner, build(True), workflow_id="wf-added")
    assert ("fetch", "max_attempts", None, 3) in [(c.node_name, c.field, c.stored, c.current) for c in exc_info.value.changes]


async def test_sqlite_manifest_survives_json_round_trip(family, make_sqlite, recorded_sleeps):
    cp = make_sqlite()
    calls: list[int] = []
    graph = _flaky_graph(calls, max_attempts=3)
    runner = _make_runner(family, checkpointer=cp)
    await _run(runner, graph, workflow_id="wf-json")

    run = await cp.get_run_async("wf-json")
    manifest = RetryPolicyManifest.from_config(run.config)
    assert manifest == RetryPolicyManifest.from_graph(graph)
