"""Work travels as data, and unexecutable work is refused or dead-lettered.

Two failures motivate every test here, both of them "accepted work nobody
alive can do":

1. A process configured a graph, submitted it, and exited. The only process
   allowed to execute could not BUILD that Definition — the submission pinned
   its identity, never a way to reconstruct it — so every row was marked
   version-incompatible and sat queued forever with no executor and no error.
2. Nothing said so. The park was silent, terminal in practice, and invisible
   to every read model.

The answer is the pattern Celery and Temporal have shipped for years: a
submission is DATA, and each worker holds a registry that resolves a name to
a constructor. So ``serve_builder()`` registers constructors beside the
instances ``serve()`` registers, ``submit(..., builder=(key, args))`` records
the address, and work nothing can execute is refused at the call site or
retired with a reason — never parked in silence.

The round trip is proven with a REAL second process that registered ONLY the
builder (``_builder_worker.py``): it cannot name the Definition it runs, so
the test cannot pass by accident of shared memory.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys

import pytest
import pytest_asyncio

from hypergraph import (
    BuilderIdentityError,
    DefinitionId,
    NoServingWorkerError,
    RunHome,
    RunHomeClient,
    RunHomeReadModel,
    RunQuery,
    RunRef,
    UnservedGraphError,
    WaitingCondition,
    serve,
)
from hypergraph.checkpointers.types import WorkflowStatus
from hypergraph.host.views import (
    DEAD_LETTER_BUILDER_FAILED,
    DEAD_LETTER_BUILDER_IDENTITY_MISMATCH,
    DEAD_LETTER_BUILDER_MISSING,
    DEAD_LETTER_UNSERVED_IDENTITY,
    DEAD_LETTERED_UPDATE_KIND,
)
from tests.test_host._builder_fixture import (
    BUILD_LOG_ENV,
    BUILDER_KEY,
    LEDGER_ENV,
    read_ledger,
    scaling_graph,
)

aiosqlite = pytest.importorskip("aiosqlite")


# === Helpers ===


@pytest_asyncio.fixture
async def home(tmp_path):
    """A FRESH world: an empty SQLite Run Home, never hand-seeded."""
    opened = RunHome.open(f"file:{tmp_path / 'runs.db'}")
    yield opened
    await opened.close()


@pytest.fixture
def builds(tmp_path, monkeypatch):
    """The append-only log of builder CALLS in this process."""
    path = tmp_path / "builds.log"
    monkeypatch.setenv(BUILD_LOG_ENV, str(path))
    return path


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


async def _state(home, workflow_id: str) -> str:
    return (await home._get_submission(workflow_id))["state"]


async def _dead_letter(home, workflow_id: str):
    """The submission once it is dead-lettered, else None."""
    submission = await home._get_submission(workflow_id)
    return submission if submission is not None and submission["state"] == "dead_letter" else None


def _reason(home, workflow_id: str) -> str | None:
    """The reason recorded on the durable ``dead_lettered`` fact."""
    return home._dead_letter_reasons_sync([workflow_id]).get(workflow_id)


# === 1. The round trip: configured here, built and executed THERE ===


class TestValuesAsDataCrossProcess:
    """The incident, reproduced and closed."""

    async def test_a_second_process_that_holds_only_the_builder_drains_the_work(self, tmp_path):
        db = str(tmp_path / "runs.db")
        ledger = str(tmp_path / "effects.log")

        # Process A configures the graph and submits. It never executes: the
        # notebook in the incident held the code and chose the values, but
        # was never the process allowed to run the work.
        home = RunHome.open(f"file:{db}")
        try:
            graph = scaling_graph({"factor": 7})
            host = serve(graph, home=home, deployment_version="v1", builders={BUILDER_KEY: scaling_graph})
            receipt = await host.submit(graph, {"x": 6}, workflow_id="wf-data", builder=(BUILDER_KEY, {"factor": 7}))
            row = await home._get_submission("wf-data")
            assert row["builder_key"] == BUILDER_KEY
            assert row["builder_args_json"] == '{"factor":7}'
            assert row["definition_name"] == "scale-x7", "the identity pins a digest of the values, never the values"
        finally:
            await home.close()

        # Process B holds no graph and no factor — only the builder.
        child = _spawn(db, ledger, str(tmp_path / "builds.log"), expected=1)
        _await_clean_exit(child)

        assert read_ledger(ledger) == [f"{child.pid}:6:42"], "the CHILD process is what executed the work"

        reopened = RunHome.open(f"file:{db}")
        try:
            outcome = await RunHomeClient(reopened).result(receipt.run_ref)
            assert outcome.status is WorkflowStatus.COMPLETED
            assert outcome.outputs == {"out": 42}
        finally:
            await reopened.close()

    async def test_a_builder_only_worker_needs_no_graph_at_serve(self, home):
        """``serve()`` with only builders is a legal deployment shape."""
        host = serve(home=home, builders={BUILDER_KEY: scaling_graph})
        assert host.builder_keys == {BUILDER_KEY}
        with pytest.raises(ValueError, match="at least one graph"):
            serve(home=home)


def _spawn(db: str, ledger: str, builds: str, *, expected: int) -> subprocess.Popen:
    """Start the real builder-only worker process on the same Run Home."""
    env = {
        **os.environ,
        LEDGER_ENV: ledger,
        BUILD_LOG_ENV: builds,
        "HYPERGRAPH_BUILDER_EXPECTED": str(expected),
        "PYTHONUNBUFFERED": "1",
    }
    return subprocess.Popen(
        [sys.executable, "-m", "tests.test_host._builder_worker", db, ledger, builds],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _await_clean_exit(child: subprocess.Popen, timeout: float = 90.0) -> None:
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:  # pragma: no cover - diagnostic path
        child.kill()
        child.wait(timeout=10)
        raise AssertionError(f"builder worker never finished; stderr={child.stderr.read()!r}") from None
    assert child.returncode == 0, f"builder worker failed: {child.stderr.read()!r}"


# === 2. The builder is a constructor, called once per address ===


class TestInstantiateOnce:
    async def test_a_batch_of_five_children_builds_its_definition_once(self, home, builds):
        """Building a configured graph is rarely cheap; a Batch pays once."""
        graph = scaling_graph({"factor": 3})
        submitter = serve(graph, home=home, deployment_version="v1", builders={BUILDER_KEY: scaling_graph})
        await submitter.submit_batch(
            graph,
            {"x": [1, 2, 3, 4, 5]},
            map_over="x",
            identity="x",
            workflow_id="drop-memo",
            builder=(BUILDER_KEY, {"factor": 3}),
        )

        # A worker that holds ONLY the builder: every one of the five claims
        # has to be resolved through it, so a missing memo would show up as
        # five constructions.
        executor = serve(home=home, deployment_version="v1", builders={BUILDER_KEY: scaling_graph})
        before = len(read_ledger(builds))
        async with _worker(executor):
            await _wait_for(lambda: _settled(executor.client, "drop-memo:5"))
        assert len(read_ledger(builds)) - before == 1

    async def test_a_second_argument_set_is_a_second_construction(self, home, builds):
        host = serve(home=home, deployment_version="v1", builders={BUILDER_KEY: scaling_graph})
        await host.submit(scaling_graph({"factor": 2}), {"x": 1}, workflow_id="wf-a", builder=(BUILDER_KEY, {"factor": 2}))
        await host.submit(scaling_graph({"factor": 5}), {"x": 1}, workflow_id="wf-b", builder=(BUILDER_KEY, {"factor": 5}))
        # Per factor: the test's own construction, then one memoized build.
        # The memo is keyed by ADDRESS, so a different argument set is a
        # different Definition and rightly builds again.
        assert read_ledger(builds) == ["factor=2", "factor=2", "factor=5", "factor=5"]

    async def test_equivalent_argument_spellings_share_one_memo(self, home, builds):
        host = serve(home=home, deployment_version="v1", builders={BUILDER_KEY: scaling_graph})
        graph = scaling_graph({"factor": 4})
        await host.submit(graph, {"x": 1}, workflow_id="wf-a", builder=(BUILDER_KEY, {"factor": 4, "unused": None}))
        await host.submit(graph, {"x": 2}, workflow_id="wf-b", builder=(BUILDER_KEY, {"unused": None, "factor": 4}))
        rows = [await home._get_submission("wf-a"), await home._get_submission("wf-b")]
        assert rows[0]["builder_args_json"] == rows[1]["builder_args_json"]
        assert read_ledger(builds) == ["factor=4", "factor=4"], "one test build, one memoized build"


# === 3. Refusal at submit: a builder address nothing can resolve ===


class TestRefusalAtSubmit:
    async def test_an_unregistered_builder_key_is_refused_before_any_row_exists(self, home):
        graph = scaling_graph({"factor": 2})
        host = serve(graph, home=home, deployment_version="v1")
        with pytest.raises(NoServingWorkerError) as caught:
            await host.submit(graph, {"x": 1}, workflow_id="wf-nobody", builder=("nobody.builds.this", {"factor": 2}))
        assert caught.value.builder_key == "nobody.builds.this"
        assert "serve_builder" in str(caught.value)
        assert await home._get_submission("wf-nobody") is None, "refusal leaves no durable trace"

    async def test_a_live_worker_that_registers_the_key_is_enough(self, tmp_path):
        """The submitter need not hold the builder — the EXECUTOR must."""
        db = f"file:{tmp_path / 'runs.db'}"
        executor_home = RunHome.open(db)
        submitter_home = RunHome.open(db)
        graph = scaling_graph({"factor": 2})
        executor = serve(home=executor_home, deployment_version="v1", builders={BUILDER_KEY: scaling_graph})
        submitter = serve(graph, home=submitter_home, deployment_version="v1")
        try:
            with pytest.raises(NoServingWorkerError):
                await submitter.submit(graph, {"x": 1}, workflow_id="wf-early", builder=(BUILDER_KEY, {"factor": 2}))
            async with _worker(executor, "w-exec"):
                await _wait_for(lambda: _registry_has(executor_home, BUILDER_KEY))
                receipt = await submitter.submit(graph, {"x": 1}, workflow_id="wf-late", builder=(BUILDER_KEY, {"factor": 2}))
                await _wait_for(lambda: _settled(submitter.client, "wf-late"))
            assert receipt.duplicate is False
        finally:
            await executor_home.close()
            await submitter_home.close()

    async def test_a_withdrawn_worker_stops_covering_work_at_once(self, home):
        host = serve(home=home, deployment_version="v1", builders={BUILDER_KEY: scaling_graph})
        submitter = serve(scaling_graph({"factor": 2}), home=home, deployment_version="v1")
        async with _worker(host, "w-brief"):
            await _wait_for(lambda: _registry_has(home, BUILDER_KEY))
        # A clean exit withdraws the registration rather than waiting out the
        # pulse window: a stopped deployment is not a slow one.
        assert await _registry_has(home, BUILDER_KEY) is False
        with pytest.raises(NoServingWorkerError):
            await submitter.submit(scaling_graph({"factor": 2}), {"x": 1}, workflow_id="wf-gone", builder=(BUILDER_KEY, {"factor": 2}))

    def test_the_sync_mirror_refuses_identically(self, home):
        graph = scaling_graph({"factor": 2})
        host = serve(graph, home=home, deployment_version="v1")
        with pytest.raises(NoServingWorkerError):
            host.submit_sync(graph, {"x": 1}, workflow_id="wf-sync", builder=("nobody.builds.this", {}))
        with pytest.raises(NoServingWorkerError):
            host.submit_batch_sync(
                graph,
                {"x": [1, 2]},
                map_over="x",
                identity="x",
                workflow_id="drop-sync",
                builder=("nobody.builds.this", {}),
            )

    async def test_a_builder_that_builds_something_else_is_refused_at_submit(self, home):
        host = serve(home=home, deployment_version="v1", builders={BUILDER_KEY: scaling_graph})
        wanted = scaling_graph({"factor": 9})
        with pytest.raises(BuilderIdentityError) as caught:
            # The arguments build scale-x2; the graph passed is scale-x9.
            await host.submit(wanted, {"x": 1}, workflow_id="wf-drift", builder=(BUILDER_KEY, {"factor": 2}))
        assert caught.value.pinned.name == "scale-x9"
        assert caught.value.built.name == "scale-x2"
        assert await home._get_submission("wf-drift") is None

    async def test_an_unserved_graph_without_a_builder_still_raises_the_old_error(self, home):
        host = serve(scaling_graph({"factor": 2}), home=home, deployment_version="v1")
        with pytest.raises(UnservedGraphError):
            await host.submit(scaling_graph({"factor": 8}), {"x": 1}, workflow_id="wf-unserved")


async def _registry_has(home, key: str) -> bool:
    coverage = await home._live_worker_coverage()
    return key in coverage.builders


async def _settled(client, workflow_id: str):
    view = await client.get(RunRef(home=client._home.uri, run_id=workflow_id))
    return view if view is not None and view.status in {WorkflowStatus.COMPLETED, WorkflowStatus.FAILED} else None


# === 4. Dead letters: retired with a reason, visible, revivable ===


class TestDeadLetterVisibility:
    async def test_work_no_live_deployment_answers_to_is_retired_with_a_reason(self, home):
        """The silent park, made loud."""
        gone = serve(scaling_graph({"factor": 11}), home=home, deployment_version="v1")
        await gone.submit(scaling_graph({"factor": 11}), {"x": 1}, workflow_id="wf-orphan")

        # A new deployment serves an entirely different Definition family.
        successor = serve(scaling_graph({"factor": 2}), home=home, deployment_version="v1")
        async with _worker(successor, "w-successor"):
            await _wait_for(lambda: _dead_letter(home, "wf-orphan"))

        assert _reason(home, "wf-orphan") == DEAD_LETTER_UNSERVED_IDENTITY
        view = await successor.client.get(RunRef(home=home.uri, run_id="wf-orphan"))
        assert view.waiting is WaitingCondition.DEAD_LETTER
        assert view.status is None

        read = await RunHomeReadModel(successor.client).get_run(view.run_ref)
        assert (read.status, read.condition) == ("failed", "dead_letter")
        assert read.dead_letter_reason == DEAD_LETTER_UNSERVED_IDENTITY
        assert read.to_dict()["dead_letter_reason"] == DEAD_LETTER_UNSERVED_IDENTITY

        listed = await RunHomeReadModel(successor.client).list_runs(RunQuery(waiting=WaitingCondition.DEAD_LETTER))
        assert [row.workflow_id for row in listed] == ["wf-orphan"]

    async def test_the_durable_stream_carries_the_reason_to_a_detached_reader(self, home):
        gone = serve(scaling_graph({"factor": 11}), home=home, deployment_version="v1")
        receipt = await gone.submit(scaling_graph({"factor": 11}), {"x": 1}, workflow_id="wf-orphan")
        successor = serve(scaling_graph({"factor": 2}), home=home, deployment_version="v1")
        async with _worker(successor, "w-successor"):
            await _wait_for(lambda: _dead_letter(home, "wf-orphan"))

        updates = [update async for update in successor.client.watch(receipt.run_ref)]
        retirement = [update for update in updates if update.kind == DEAD_LETTERED_UPDATE_KIND]
        assert len(retirement) == 1
        assert retirement[0].durable is True
        assert retirement[0].payload["reason"] == DEAD_LETTER_UNSERVED_IDENTITY
        assert retirement[0].payload["definition_id"]["name"] == "scale-x11"

    async def test_a_dead_letter_is_settled_so_a_batch_watch_can_end(self, home):
        gone = serve(scaling_graph({"factor": 11}), home=home, deployment_version="v1")
        receipt = await gone.submit_batch(
            scaling_graph({"factor": 11}),
            {"x": [1, 2]},
            map_over="x",
            identity="x",
            workflow_id="drop-orphan",
        )
        successor = serve(scaling_graph({"factor": 2}), home=home, deployment_version="v1")
        async with _worker(successor, "w-successor"):
            view = await _wait_for(lambda: _settled_batch(successor.client, receipt.batch_ref))

        assert view.counts["dead_letter"] == 2
        assert sum(view.counts.values()) == 2, "every manifest item is accounted exactly once"
        assert view.outcomes == {"1": "dead_letter", "2": "dead_letter"}
        assert view.tolerance_tripped is False, "a missing deployment is not the Batch's work failing"

    async def test_rerun_revives_a_dead_letter(self, home):
        gone = serve(scaling_graph({"factor": 11}), home=home, deployment_version="v1")
        receipt = await gone.submit(scaling_graph({"factor": 11}), {"x": 6}, workflow_id="wf-orphan")
        successor = serve(scaling_graph({"factor": 2}), home=home, deployment_version="v1")
        async with _worker(successor, "w-successor"):
            await _wait_for(lambda: _dead_letter(home, "wf-orphan"))

        # The deployment that CAN run it arrives; rerun is how the work comes
        # back, exactly as it does for recovery-exhausted work.
        revived = serve(scaling_graph({"factor": 11}), home=home, deployment_version="v1")
        repeat = await revived.client.rerun(receipt.run_ref)
        assert repeat.workflow_id == "wf-orphan-retry-1"
        async with _worker(revived, "w-revived"):
            outcome = await _wait_for(lambda: _completed(revived.client, repeat.run_ref))
        assert outcome.outputs == {"out": 66}

    async def test_a_builder_address_nobody_registers_names_its_own_reason(self, home):
        """Distinguishable from an unserved identity, because the fix differs."""
        graph = scaling_graph({"factor": 2})
        host = serve(graph, home=home, deployment_version="v1", builders={BUILDER_KEY: scaling_graph})
        await host.submit(graph, {"x": 1}, workflow_id="wf-key", builder=(BUILDER_KEY, {"factor": 2}))
        # The deployment that drains it forgot the builder AND the graph.
        successor = serve(scaling_graph({"factor": 3}), home=home, deployment_version="v1")
        async with _worker(successor, "w-successor"):
            await _wait_for(lambda: _dead_letter(home, "wf-key"))
        assert _reason(home, "wf-key") == DEAD_LETTER_BUILDER_MISSING

    async def test_a_builder_that_drifted_dead_letters_rather_than_executing(self, home):
        """A pinned identity is never reinterpreted, even by a live builder."""
        graph = scaling_graph({"factor": 2})
        host = serve(graph, home=home, deployment_version="v1", builders={BUILDER_KEY: scaling_graph})
        await host.submit(graph, {"x": 1}, workflow_id="wf-drift", builder=(BUILDER_KEY, {"factor": 2}))

        # The builder is replaced by one that answers with different code.
        drifted = serve(home=home, deployment_version="v1", builders={BUILDER_KEY: lambda args: scaling_graph({"factor": 99})})
        async with _worker(drifted, "w-drifted"):
            await _wait_for(lambda: _dead_letter(home, "wf-drift"))
        assert _reason(home, "wf-drift") == DEAD_LETTER_BUILDER_IDENTITY_MISMATCH
        assert home.get_run("wf-drift") is None, "nothing executed under the wrong topology"

    async def test_a_builder_that_raises_is_retired_rather_than_holding_its_claim(self, home):
        graph = scaling_graph({"factor": 2})
        host = serve(graph, home=home, deployment_version="v1", builders={BUILDER_KEY: scaling_graph})
        await host.submit(graph, {"x": 1}, workflow_id="wf-boom", builder=(BUILDER_KEY, {"factor": 2}))

        def explode(args):
            raise RuntimeError("the corpus is not open")

        broken = serve(home=home, deployment_version="v1", builders={BUILDER_KEY: explode})
        async with _worker(broken, "w-broken"):
            await _wait_for(lambda: _dead_letter(home, "wf-boom"))
        assert _reason(home, "wf-boom") == DEAD_LETTER_BUILDER_FAILED


async def _settled_batch(client, batch_ref):
    view = await client.get(batch_ref)
    return view if view is not None and view.settled else None


async def _completed(client, ref: RunRef):
    outcome = await client.result(ref)
    return outcome if outcome is not None and outcome.status is WorkflowStatus.COMPLETED else None


# === 5. A rolling deployment still parks; only a dead address dies ===


class TestParkingIsNotRetirement:
    async def test_a_version_skew_on_a_served_name_still_parks_for_accepts(self, home):
        """``accepts=`` must keep working, so a same-NAME skew is a wait."""
        graph = scaling_graph({"factor": 2})
        old = serve(graph, home=home, deployment_version="old")
        receipt = await old.submit(graph, {"x": 4}, workflow_id="wf-skew")

        new = serve(graph, home=home, deployment_version="new")
        async with _worker(new, "w-new"):
            await _wait_for(lambda: _incompatible(home, "wf-skew"))
            view = await new.client.get(receipt.run_ref)
            assert view.waiting is WaitingCondition.VERSION_INCOMPATIBLE
        assert await _state(home, "wf-skew") == "pending", "parked, not retired"

        drainer = serve(graph, home=home, deployment_version="new", accepts=(DefinitionId("scale-x2", "old", graph.structural_hash),))
        async with _worker(drainer, "w-drainer"):
            outcome = await _wait_for(lambda: _completed(drainer.client, receipt.run_ref))
        assert outcome.outputs == {"out": 8}


async def _incompatible(home, workflow_id: str):
    submission = await home._get_submission(workflow_id)
    return submission if submission is not None and submission["compat_state"] == "incompatible" else None


# === 6. Mixed forms drain side by side ===


class TestMixedForms:
    async def test_one_worker_drains_instance_form_and_builder_form_together(self, home, builds):
        """Both doors, one queue, one claim scan, no ordering privilege."""
        served = scaling_graph({"factor": 2})
        addressed = scaling_graph({"factor": 3})
        host = serve(served, home=home, deployment_version="v1", builders={BUILDER_KEY: scaling_graph})

        old_form = await host.submit_batch(served, {"x": [1, 2]}, map_over="x", identity="x", workflow_id="drop-old")
        new_form = await host.submit_batch(
            addressed,
            {"x": [1, 2]},
            map_over="x",
            identity="x",
            workflow_id="drop-new",
            builder=(BUILDER_KEY, {"factor": 3}),
        )
        async with _worker(host):
            old_view = await _wait_for(lambda: _settled_batch(host.client, old_form.batch_ref))
            new_view = await _wait_for(lambda: _settled_batch(host.client, new_form.batch_ref))

        assert old_view.outcomes == {"1": "completed", "2": "completed"}
        assert new_view.outcomes == {"1": "completed", "2": "completed"}
        old_children = [await home._get_submission(f"drop-old:{key}") for key in ("1", "2")]
        new_children = [await home._get_submission(f"drop-new:{key}") for key in ("1", "2")]
        assert [child["builder_key"] for child in old_children] == [None, None]
        assert [child["builder_key"] for child in new_children] == [BUILDER_KEY, BUILDER_KEY]

        outcomes = await host.client.result(new_form.batch_ref)
        assert {key: item.outputs for key, item in outcomes.items.items()} == {"1": {"out": 3}, "2": {"out": 6}}


# === 7. Registration hygiene ===


class TestBuilderRegistration:
    def test_a_key_must_be_a_non_empty_string_and_a_builder_must_be_callable(self, home):
        host = serve(scaling_graph({"factor": 2}), home=home)
        with pytest.raises(ValueError, match="non-empty string key"):
            host.serve_builder("", scaling_graph)
        with pytest.raises(TypeError, match="callable"):
            host.serve_builder("k", "not-callable")

    async def test_a_builder_argument_must_be_a_key_and_mapping_pair(self, home):
        graph = scaling_graph({"factor": 2})
        host = serve(graph, home=home, builders={BUILDER_KEY: scaling_graph})
        with pytest.raises(TypeError, match=r"\(key, args\) pair"):
            await host.submit(graph, {"x": 1}, builder=BUILDER_KEY)
        with pytest.raises(TypeError, match="must be a Mapping"):
            await host.submit(graph, {"x": 1}, builder=(BUILDER_KEY, ["factor", 2]))
        with pytest.raises(ValueError, match="non-empty string key"):
            await host.submit(graph, {"x": 1}, builder=("", {}))

    async def test_re_registering_a_key_replaces_what_it_builds(self, home):
        host = serve(home=home, deployment_version="v1", builders={BUILDER_KEY: scaling_graph})
        await host.submit(scaling_graph({"factor": 6}), {"x": 1}, workflow_id="wf-a", builder=(BUILDER_KEY, {"factor": 6}))

        host.serve_builder(BUILDER_KEY, lambda args: scaling_graph({"factor": 99}))
        with pytest.raises(BuilderIdentityError):
            # The replacement answers every address with scale-x99, and the
            # identity check is what makes replacement safe rather than a way
            # to change what a submission runs.
            await host.submit(scaling_graph({"factor": 7}), {"x": 1}, workflow_id="wf-c", builder=(BUILDER_KEY, {"factor": 7}))

    async def test_the_runtime_registers_builders_and_starts_its_worker(self, tmp_path):
        from hypergraph import HostRuntime

        runtime = HostRuntime(tmp_path / "runtime.db", deployment_version="v1")
        try:
            host = await runtime.serving_builder(BUILDER_KEY, scaling_graph)
            assert host.builder_keys == {BUILDER_KEY}
            graph = scaling_graph({"factor": 4})
            receipt = await host.submit(graph, {"x": 5}, workflow_id="wf-runtime", builder=(BUILDER_KEY, {"factor": 4}))
            outcome = await _wait_for(lambda: _completed(runtime.client, receipt.run_ref))
            assert outcome.outputs == {"out": 20}
        finally:
            await runtime.close()
