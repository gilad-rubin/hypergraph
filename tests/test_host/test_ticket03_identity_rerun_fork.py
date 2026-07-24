"""Durable Host V1 — ticket 03: truthful run identity, dedup, rerun, fork.

Covers: pinned DefinitionId visibility, start-fingerprint dedup with
distinct typed conflicts, version-incompatible refusal with accepts=
drain, client.rerun retry lineage, host.fork migration lineage with a
recorded reason, and sync/async parity for the executing paths.
"""

import asyncio
import contextlib
import json
import re

import pytest
import pytest_asyncio

from hypergraph import (
    AlreadyTerminalError,
    AsyncRunner,
    DefinitionId,
    ForkCompatibilityError,
    Graph,
    RerunError,
    RunHome,
    RunRef,
    SyncRunner,
    WaitingCondition,
    WorkflowIdConflictError,
    node,
    serve,
)
from hypergraph.checkpointers.types import WorkflowStatus

aiosqlite = pytest.importorskip("aiosqlite")


# === Helpers (mirrors ticket-02 conventions) ===


def _sync_graph(name: str) -> Graph:
    @node(output_name="out")
    def compute(x: int) -> int:
        return x + 1

    return Graph([compute], name=name).with_runner(SyncRunner())


def _async_graph(name: str) -> Graph:
    @node(output_name="out")
    async def compute(x: int) -> int:
        return x + 1

    return Graph([compute], name=name).with_runner(AsyncRunner())


def _flaky_sync_graph(name: str, calls: dict, should_fail: dict) -> Graph:
    """Two-node graph whose second node fails until should_fail flips."""

    @node(output_name="seed")
    def seed(x: int) -> int:
        calls["seed"] += 1
        return x

    @node(output_name="out")
    def flaky(seed: int) -> int:
        calls["flaky"] += 1
        if should_fail["v"]:
            raise RuntimeError("transient")
        return seed * 10

    return Graph([seed, flaky], name=name).with_runner(SyncRunner())


def _flaky_async_graph(name: str, calls: dict, should_fail: dict) -> Graph:
    @node(output_name="seed")
    def seed(x: int) -> int:
        calls["seed"] += 1
        return x

    @node(output_name="out")
    async def flaky(seed: int) -> int:
        calls["flaky"] += 1
        if should_fail["v"]:
            raise RuntimeError("transient")
        return seed * 10

    return Graph([seed, flaky], name=name).with_runner(AsyncRunner())


def _home_uri(tmp_path, filename: str = "runs.db") -> str:
    return f"file:{tmp_path / filename}"


async def _wait_for(check, timeout: float = 15.0, interval: float = 0.02):
    """Poll an async zero-arg callable until it returns a truthy value."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        value = await check()
        if value:
            return value
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(interval)


@contextlib.asynccontextmanager
async def _worker(host, worker_id: str = "w-test", **kwargs):
    """Run work_forever as a task; shut it down cleanly on exit."""
    task = asyncio.create_task(host.work_forever(worker_id, **kwargs))
    try:
        yield task
    finally:
        host.shutdown()
        await asyncio.wait_for(task, timeout=20)


async def _terminal_view(client, ref):
    view = await client.get(ref)
    if view is not None and view.status in {
        WorkflowStatus.COMPLETED,
        WorkflowStatus.FAILED,
        WorkflowStatus.PARTIAL,
        WorkflowStatus.STOPPED,
    }:
        return view
    return None


async def _compat_state(home, workflow_id, compat):
    submission = home._get_submission_sync(workflow_id)
    return submission is not None and submission["compat_state"] == compat


@pytest_asyncio.fixture
async def home(tmp_path):
    h = RunHome.open(_home_uri(tmp_path))
    yield h
    await h.close()


# === 1. Pinned Definition identity is visible ===


class TestDefinitionIdValue:
    def test_round_trip_frozen_and_root_export(self):
        identity = DefinitionId("ingest", "2026.06.1", "ab" * 32)
        data = identity.to_dict()
        json.dumps(data)  # JSON-serializable
        assert DefinitionId.from_dict(json.loads(json.dumps(data))) == identity
        with pytest.raises(AttributeError):
            identity.name = "other"  # frozen
        with pytest.raises(ValueError, match="missing key"):
            DefinitionId.from_dict({"name": "x"})

    async def test_pinned_identity_visible_on_view_and_submission(self, home):
        graph = _sync_graph("dbl")
        host = serve(graph, home=home, deployment_version="2026.07.3")
        receipt = await host.submit("dbl", {"x": 1}, workflow_id="wf-pin")

        expected = DefinitionId("dbl", "2026.07.3", graph.structural_hash)
        submission = home._get_submission_sync("wf-pin")
        assert (submission["definition_name"], submission["def_version"], submission["def_struct_hash"]) == (
            expected.name,
            expected.deployment_version,
            expected.structural_hash,
        )
        assert submission["fingerprint"]

        view = await host.client.get(receipt.run_ref)
        assert view.definition_id == expected

        async with _worker(host):
            view = await _wait_for(lambda: _terminal_view(host.client, receipt.run_ref))
        assert view.definition_id == expected
        assert view.retry_of is None
        assert view.forked_from is None


# === 2. Fingerprint dedup with distinct typed conflicts ===


class TestFingerprintDedup:
    async def test_identical_nonterminal_resubmit_dedupes(self, home):
        host = serve(_sync_graph("dbl"), home=home, deployment_version="v1")
        first = await host.submit("dbl", {"x": 1}, workflow_id="wf-dup")
        second = await host.submit("dbl", {"x": 1}, workflow_id="wf-dup")
        assert second.duplicate is True
        assert second.run_ref == first.run_ref
        # No new submission or update rows were written.
        assert len(home._read_run_updates_sync("wf-dup")) == 1

    async def test_terminal_reuse_raises_already_terminal(self, home):
        host = serve(_sync_graph("dbl"), home=home)
        receipt = await host.submit("dbl", {"x": 1}, workflow_id="wf-term")
        async with _worker(host):
            await _wait_for(lambda: _terminal_view(host.client, receipt.run_ref))
        # Identical resubmission of a terminal run is a terminal conflict.
        with pytest.raises(AlreadyTerminalError):
            await host.submit("dbl", {"x": 1}, workflow_id="wf-term")

    async def test_different_inputs_raise_workflow_id_conflict(self, home):
        host = serve(_sync_graph("dbl"), home=home)
        await host.submit("dbl", {"x": 1}, workflow_id="wf-conf")
        with pytest.raises(WorkflowIdConflictError, match="inputs"):
            await host.submit("dbl", {"x": 2}, workflow_id="wf-conf")

    async def test_different_start_at_raises_workflow_id_conflict(self, home):
        host = serve(_sync_graph("dbl"), home=home)
        await host.submit("dbl", {"x": 1}, workflow_id="wf-sched", start_at="2030-01-01T00:00:00+00:00")
        with pytest.raises(WorkflowIdConflictError, match="start_at"):
            await host.submit("dbl", {"x": 1}, workflow_id="wf-sched", start_at="2031-01-01T00:00:00+00:00")
        # Same start_at dedupes.
        dup = await host.submit("dbl", {"x": 1}, workflow_id="wf-sched", start_at="2030-01-01T00:00:00+00:00")
        assert dup.duplicate is True

    async def test_mismatch_after_terminal_is_already_terminal(self, home):
        """Terminal reuse wins over fingerprint mismatch (order matters)."""
        host = serve(_sync_graph("dbl"), home=home)
        receipt = await host.submit("dbl", {"x": 1}, workflow_id="wf-order")
        async with _worker(host):
            await _wait_for(lambda: _terminal_view(host.client, receipt.run_ref))
        with pytest.raises(AlreadyTerminalError):
            await host.submit("dbl", {"x": 999}, workflow_id="wf-order")

    async def test_conflict_error_carries_workflow_id(self, home):
        host = serve(_sync_graph("dbl"), home=home)
        await host.submit("dbl", {"x": 1}, workflow_id="wf-carry")
        with pytest.raises(WorkflowIdConflictError) as excinfo:
            await host.submit("dbl", {"x": 2}, workflow_id="wf-carry")
        assert excinfo.value.workflow_id == "wf-carry"


class TestFingerprintNormalization:
    async def test_dict_key_order_dedupes_identically(self, home):
        host = serve(_sync_graph("dbl"), home=home)
        first = await host.submit("dbl", {"a": 1, "b": {"c": 2, "d": 3}}, workflow_id="wf-norm")
        second = await host.submit("dbl", {"b": {"d": 3, "c": 2}, "a": 1}, workflow_id="wf-norm")
        assert second.duplicate is True
        assert second.run_ref == first.run_ref
        assert len(home._read_run_updates_sync("wf-norm")) == 1

    def test_equivalent_json_values_share_a_fingerprint(self):
        from hypergraph.host.fingerprint import start_fingerprint

        identity = DefinitionId("dbl", "v1", "hash")
        fp_a = start_fingerprint(identity, '{"x": 1, "y": [1, 2]}', None)
        fp_b = start_fingerprint(identity, '{"y":[1,2],"x":1}', None)
        assert fp_a == fp_b
        assert fp_a != start_fingerprint(identity, '{"x": 1}', None)
        assert fp_a != start_fingerprint(identity, '{"x": 1, "y": [1, 2]}', "2030-01-01T00:00:00+00:00")
        assert fp_a != start_fingerprint(DefinitionId("dbl", "v2", "hash"), '{"x": 1, "y": [1, 2]}', None)


# === 3. Version refusal and accepts= drain ===


class TestVersionRefusal:
    async def test_incompatible_refused_then_accepts_drains(self, tmp_path, home, caplog):
        graph = _sync_graph("dbl")
        old_id = DefinitionId("dbl", "old", graph.structural_hash)

        host_old = serve(graph, home=home, deployment_version="old")
        receipt = await host_old.submit("dbl", {"x": 1}, workflow_id="wf-parked")
        assert home._get_submission_sync("wf-parked")["def_version"] == "old"

        # A new deployment refuses the old pinned identity: the submission
        # stays parked, the view names the typed waiting condition, and the
        # worker logs a warning naming the identity it cannot serve.
        host_new = serve(graph, home=home, deployment_version="new")
        with caplog.at_level("WARNING", logger="hypergraph.host"):
            async with _worker(host_new, "w-new"):
                await _wait_for(lambda: _compat_state(home, "wf-parked", "incompatible"))
                view = await host_new.client.get(receipt.run_ref)
                assert view.waiting is WaitingCondition.VERSION_INCOMPATIBLE
                assert view.status is None
        assert home._get_submission_sync("wf-parked")["state"] == "pending"
        assert home.get_run("wf-parked") is None
        refusal_warnings = [r for r in caplog.records if r.name == "hypergraph.host" and r.levelname == "WARNING"]
        assert refusal_warnings, "expected a worker warning naming the refused identity"
        assert any("wf-parked" in r.getMessage() and "old" in r.getMessage() for r in refusal_warnings)

        # An accepts entry with the WRONG structural hash is rejected at
        # serve() time — an undrainable declaration would park forever.
        wrong = DefinitionId("dbl", "old", "0" * 64)
        with pytest.raises(ValueError, match="structural_hash"):
            serve(graph, home=home, deployment_version="new", accepts=(wrong,))
        # An accepts entry naming an unserved Definition is rejected too.
        unserved = DefinitionId("nosuchdef", "old", graph.structural_hash)
        with pytest.raises(ValueError, match="does not serve"):
            serve(graph, home=home, deployment_version="new", accepts=(unserved,))

        # Declaring the exact prior identity drains the parked run.
        host_ok = serve(graph, home=home, deployment_version="new", accepts=(old_id,))
        async with _worker(host_ok, "w-ok"):
            view = await _wait_for(lambda: _terminal_view(host_ok.client, receipt.run_ref))
        assert view.status == WorkflowStatus.COMPLETED
        assert view.definition_id == old_id  # pinned identity never rewrites
        assert home.values("wf-parked")["out"] == 2

    async def test_serve_validates_accepts_entries(self):
        home = RunHome.open(":memory:")
        try:
            with pytest.raises(TypeError, match="DefinitionId"):
                serve(_sync_graph("dbl"), home=home, accepts=("not-a-definition-id",))
            with pytest.raises(TypeError, match="DefinitionId"):
                serve(_sync_graph("dbl"), home=home, accepts=({"name": "dbl", "deployment_version": "old", "structural_hash": "h"},))
        finally:
            await home.close()


# === 4. client.rerun: repetition with retry lineage ===


class TestRerun:
    async def test_rerun_creates_retry_lineage_and_reexecutes(self, tmp_path, home):
        calls = {"seed": 0, "flaky": 0}
        should_fail = {"v": True}
        graph = _flaky_sync_graph("flaky-def", calls, should_fail)
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await host.submit("flaky-def", {"x": 5}, workflow_id="wf-src")
        async with _worker(host):
            view = await _wait_for(lambda: _terminal_view(host.client, receipt.run_ref))
        assert view.status in (WorkflowStatus.FAILED, WorkflowStatus.PARTIAL)
        assert calls == {"seed": 1, "flaky": 1}

        should_fail["v"] = False
        rerun_receipt = await host.client.rerun(receipt.run_ref)
        assert rerun_receipt.workflow_id == "wf-src-retry-1"
        assert rerun_receipt.duplicate is False
        assert rerun_receipt.run_ref == RunRef(home=home.uri, run_id="wf-src-retry-1")

        # The new submission pins the SOURCE Definition identity and inputs.
        source_sub = home._get_submission_sync("wf-src")
        retry_sub = home._get_submission_sync("wf-src-retry-1")
        assert retry_sub["retry_of"] == "wf-src"
        assert retry_sub["forked_from"] is None
        assert retry_sub["inputs_json"] == source_sub["inputs_json"]
        assert (retry_sub["definition_name"], retry_sub["def_version"], retry_sub["def_struct_hash"]) == (
            source_sub["definition_name"],
            source_sub["def_version"],
            source_sub["def_struct_hash"],
        )

        async with _worker(host, "w-2"):
            retry_view = await _wait_for(lambda: _terminal_view(host.client, rerun_receipt.run_ref))

        assert retry_view.status == WorkflowStatus.COMPLETED
        assert calls["flaky"] == 2  # the failed node re-executed
        assert home.values("wf-src-retry-1")["out"] == 50  # source inputs, no override

        run = home.get_run("wf-src-retry-1")
        assert run.retry_of == "wf-src"
        assert run.retry_index == 1
        # Lineage never merges at the client surface.
        assert retry_view.retry_of == "wf-src"
        assert retry_view.forked_from is None
        assert retry_view.definition_id == view.definition_id

        # A second rerun derives the next index after the first settled.
        second = await host.client.rerun(receipt.run_ref)
        assert second.workflow_id == "wf-src-retry-2"
        async with _worker(host, "w-3"):
            await _wait_for(lambda: _terminal_view(host.client, second.run_ref))
        run2 = home.get_run("wf-src-retry-2")
        assert run2.retry_of == "wf-src"
        assert run2.retry_index == 2

    async def test_rerun_rejects_nonterminal_and_unknown_sources(self, home):
        host = serve(_sync_graph("dbl"), home=home)
        receipt = await host.submit("dbl", {"x": 1}, workflow_id="wf-pending")
        with pytest.raises(RerunError, match="not terminal"):
            await host.client.rerun(receipt.run_ref)
        with pytest.raises(RerunError, match="no such run"):
            await host.client.rerun(RunRef(home=home.uri, run_id="wf-nope"))
        assert RerunError.__mro__[1].__name__ == "HostError"

    async def test_rerun_accepts_no_input_override(self, home):
        host = serve(_sync_graph("dbl"), home=home)
        receipt = await host.submit("dbl", {"x": 1}, workflow_id="wf-noov")
        async with _worker(host):
            await _wait_for(lambda: _terminal_view(host.client, receipt.run_ref))
        with pytest.raises(TypeError):
            await host.client.rerun(receipt.run_ref, inputs={"x": 9})

    async def test_rerun_sync_mirror(self, home):
        host = serve(_sync_graph("dbl"), home=home)
        receipt = host.submit_sync("dbl", {"x": 1}, workflow_id="wf-rsync")
        async with _worker(host):
            await _wait_for(lambda: _terminal_view(host.client, receipt.run_ref))
        rerun_receipt = host.client.rerun_sync(receipt.run_ref)
        assert rerun_receipt.workflow_id == "wf-rsync-retry-1"
        assert home._get_submission_sync("wf-rsync-retry-1")["retry_of"] == "wf-rsync"
        with pytest.raises(RerunError):
            host.client.rerun_sync(RunRef(home=home.uri, run_id="wf-rsync-retry-1"))  # not terminal yet


# === 5. host.fork: migration with fork lineage and recorded reason ===


class TestFork:
    async def test_fork_migrates_with_lineage_and_reason(self, tmp_path, home):
        calls = {"seed": 0, "flaky": 0}
        should_fail = {"v": True}
        graph = _flaky_sync_graph("olddef", calls, should_fail)
        # Same node topology under a second Definition name → same structural hash.
        target = _flaky_sync_graph("newdef", calls, should_fail)
        assert target.structural_hash == graph.structural_hash

        host = serve(graph, target, home=home, deployment_version="2026.07.3")
        receipt = await host.submit("olddef", {"x": 5}, workflow_id="wf-fork-src")
        async with _worker(host):
            view = await _wait_for(lambda: _terminal_view(host.client, receipt.run_ref))
        assert view.status in (WorkflowStatus.FAILED, WorkflowStatus.PARTIAL)

        should_fail["v"] = False
        fork_receipt = await host.fork(receipt.run_ref, into="newdef", reason="migrate to 2026.07.3")
        assert re.fullmatch(r"wf-fork-src-fork-[0-9a-f]{6}", fork_receipt.workflow_id)
        assert fork_receipt.duplicate is False

        fork_sub = home._get_submission_sync(fork_receipt.workflow_id)
        assert fork_sub["forked_from"] == "wf-fork-src"
        assert fork_sub["fork_reason"] == "migrate to 2026.07.3"
        assert fork_sub["retry_of"] is None
        # Pinned to the TARGET Definition identity.
        assert (fork_sub["definition_name"], fork_sub["def_version"], fork_sub["def_struct_hash"]) == (
            "newdef",
            "2026.07.3",
            target.structural_hash,
        )

        async with _worker(host, "w-2"):
            fork_view = await _wait_for(lambda: _terminal_view(host.client, fork_receipt.run_ref))

        assert fork_view.status == WorkflowStatus.COMPLETED
        assert calls["flaky"] == 2  # unfinished work re-executed under the new Definition
        assert home.values(fork_receipt.workflow_id)["out"] == 50

        run = home.get_run(fork_receipt.workflow_id)
        assert run.forked_from == "wf-fork-src"
        assert run.retry_of is None  # lineage never merges
        assert fork_view.forked_from == "wf-fork-src"
        assert fork_view.retry_of is None
        assert fork_view.definition_id == DefinitionId("newdef", "2026.07.3", target.structural_hash)

    async def test_fork_rejects_incompatible_target(self, home):
        graph = _sync_graph("dbl")

        @node(output_name="other_out")
        def compute(x: int) -> int:
            return x + 1

        different = Graph([compute], name="different").with_runner(SyncRunner())
        assert different.structural_hash != graph.structural_hash

        host = serve(graph, different, home=home)
        receipt = await host.submit("dbl", {"x": 1}, workflow_id="wf-incompat")
        with pytest.raises(ForkCompatibilityError) as excinfo:
            await host.fork(receipt.run_ref, into="different", reason="schema change")
        assert excinfo.value.source.name == "dbl"
        assert excinfo.value.target.name == "different"

    async def test_fork_validates_reason_and_target(self, home):
        host = serve(_sync_graph("dbl"), home=home)
        receipt = await host.submit("dbl", {"x": 1}, workflow_id="wf-val")
        with pytest.raises(ValueError, match="reason"):
            await host.fork(receipt.run_ref, into="dbl", reason="")
        with pytest.raises(ValueError, match="reason"):
            await host.fork(receipt.run_ref, into="dbl", reason="   ")
        with pytest.raises(ValueError, match="Unknown definition"):
            await host.fork(receipt.run_ref, into="nosuchdef", reason="migrate")

    async def test_fork_sync_mirror(self, home):
        graph = _sync_graph("dbl")
        other = _sync_graph("dbl2")
        host = serve(graph, other, home=home, deployment_version="v2")
        receipt = host.submit_sync("dbl", {"x": 1}, workflow_id="wf-fsync")
        fork_receipt = host.fork_sync(receipt.run_ref, into="dbl2", reason="migrate to v2")
        assert re.fullmatch(r"wf-fsync-fork-[0-9a-f]{6}", fork_receipt.workflow_id)
        fork_sub = home._get_submission_sync(fork_receipt.workflow_id)
        assert fork_sub["forked_from"] == "wf-fsync"
        assert fork_sub["fork_reason"] == "migrate to v2"
        assert (fork_sub["definition_name"], fork_sub["def_version"]) == ("dbl2", "v2")


# === 6. Sync + async Definition parity for executing paths ===


class TestSyncAsyncParity:
    async def test_async_dedup_rerun_and_fork(self, tmp_path, home):
        calls = {"seed": 0, "flaky": 0}
        should_fail = {"v": True}
        graph = _flaky_async_graph("async-old", calls, should_fail)
        target = _flaky_async_graph("async-new", calls, should_fail)
        host = serve(graph, target, home=home, deployment_version="v1")

        # Dedup: identical resubmission uses the existing run.
        first = await host.submit("async-old", {"x": 5}, workflow_id="wf-a-src")
        dup = await host.submit("async-old", {"x": 5}, workflow_id="wf-a-src")
        assert dup.duplicate is True and dup.run_ref == first.run_ref
        with pytest.raises(WorkflowIdConflictError):
            await host.submit("async-old", {"x": 6}, workflow_id="wf-a-src")

        async with _worker(host):
            view = await _wait_for(lambda: _terminal_view(host.client, first.run_ref))
        assert view.status in (WorkflowStatus.FAILED, WorkflowStatus.PARTIAL)

        should_fail["v"] = False

        # Rerun: retry lineage through the async runner.
        rerun_receipt = await host.client.rerun(first.run_ref)
        assert rerun_receipt.workflow_id == "wf-a-src-retry-1"

        # Fork: fork lineage through the async runner.
        fork_receipt = await host.fork(first.run_ref, into="async-new", reason="async migration")

        async with _worker(host, "w-2"):
            retry_view = await _wait_for(lambda: _terminal_view(host.client, rerun_receipt.run_ref))
            fork_view = await _wait_for(lambda: _terminal_view(host.client, fork_receipt.run_ref))

        assert retry_view.status == WorkflowStatus.COMPLETED
        assert retry_view.retry_of == "wf-a-src"
        assert retry_view.forked_from is None
        retry_run = home.get_run("wf-a-src-retry-1")
        assert retry_run.retry_of == "wf-a-src" and retry_run.retry_index == 1

        assert fork_view.status == WorkflowStatus.COMPLETED
        assert fork_view.forked_from == "wf-a-src"
        assert fork_view.retry_of is None
        fork_run = home.get_run(fork_receipt.workflow_id)
        assert fork_run.forked_from == "wf-a-src" and fork_run.retry_of is None
        assert home.values(fork_receipt.workflow_id)["out"] == 50
        assert home.values("wf-a-src-retry-1")["out"] == 50
        assert host.worker_errors == []
