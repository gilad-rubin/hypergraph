"""Provider-limit acquisition follows ONE global order across limiter instances.

"Graph budgets first, then the node budget" orders the limiters *within* a
single execution path. It says nothing about how two different paths order
the *same two* limiters against each other, so two individually legal graphs

    graph_ab = Graph([node(provider_limit=beta)]).with_provider_limit(alpha)
    graph_ba = Graph([node(provider_limit=alpha)]).with_provider_limit(beta)

acquire ``(alpha, beta)`` and ``(beta, alpha)``. Run them at the same time and
each holds the permit the other is waiting for: a circular wait that no
timeout ends, because a provider-permit wait is deliberately not an attempt.

The fix is the classic one for lock-ordering deadlock: a stable total order
over the limiter instances themselves (their construction rank), applied at
every acquisition site. These tests are the proof, and every one of them is
bounded so a regression fails instead of hanging CI.
"""

import asyncio
import threading
import time

import pytest

from hypergraph import AsyncRunner, Graph, ProcessLocalLimiter, SyncRunner, node
from hypergraph.runners._shared.provider_limits import provider_permits

# Long enough that a slow machine never trips it, short enough that a real
# regression reports in seconds instead of wedging the session.
DEADLOCK_TIMEOUT = 10.0


def _parked(*limiters: ProcessLocalLimiter) -> int:
    """Takers queued across ``limiters``, whichever limiter they landed on.

    The count is deliberately order-agnostic: which limiter a path parks on
    is exactly what this fix changes, so the tests must not assume it.
    """
    return sum(len(limiter._waiters) for limiter in limiters)


async def _await_parked(limiters: tuple[ProcessLocalLimiter, ...], count: int) -> None:
    async def _poll() -> None:
        while _parked(*limiters) < count:
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), timeout=DEADLOCK_TIMEOUT)


def _wait_parked(limiters: tuple[ProcessLocalLimiter, ...], count: int) -> None:
    deadline = time.monotonic() + DEADLOCK_TIMEOUT
    while time.monotonic() < deadline:
        if _parked(*limiters) >= count:
            return
        time.sleep(0.001)
    raise AssertionError(f"only {_parked(*limiters)} of {count} takers parked within {DEADLOCK_TIMEOUT}s")


def _limited_graph(name: str, graph_limit, node_limit):
    """One-node graph carrying a graph-scope and a node-scope budget."""

    @node(output_name=f"{name}_out", provider_limit=node_limit)
    async def work(x: int) -> int:
        return x

    graph = Graph([work], name=name)
    return graph.with_provider_limit(graph_limit) if graph_limit is not None else graph


def _limited_sync_graph(name: str, graph_limit, node_limit):
    @node(output_name=f"{name}_out", provider_limit=node_limit)
    def work(x: int) -> int:
        return x

    graph = Graph([work], name=name)
    return graph.with_provider_limit(graph_limit) if graph_limit is not None else graph


async def _run_all_parked_then_release(graphs, held: tuple[ProcessLocalLimiter, ...]):
    """Park one run per graph on ``held``, release everything, await them all.

    Every limiter in ``held`` is taken by this task first, so each run is
    forced to queue; releasing them hands each run its first permit at
    roughly the same moment, which is precisely the window where conflicting
    acquisition orders close a cycle.
    """
    runner = AsyncRunner()

    async def _drive():
        tasks = []
        for index, graph in enumerate(graphs):
            tasks.append(asyncio.create_task(runner.run(graph, {"x": index})))
            await _await_parked(held, len(tasks))
        return tasks

    async def _body():
        taken: list[ProcessLocalLimiter] = []
        try:
            for limiter in held:
                await limiter.__aenter__()
                taken.append(limiter)
            tasks = await _drive()
        finally:
            for limiter in reversed(taken):
                await limiter.__aexit__(None, None, None)
        return await asyncio.gather(*tasks)

    return await asyncio.wait_for(_body(), timeout=DEADLOCK_TIMEOUT)


def _run_all_parked_then_release_sync(graphs, held: tuple[ProcessLocalLimiter, ...]):
    results: list[dict] = [None] * len(graphs)  # type: ignore[list-item]
    errors: list[BaseException] = []
    threads: list[threading.Thread] = []

    def _target(index: int, graph) -> None:
        try:
            results[index] = dict(SyncRunner().run(graph, {"x": index}).values)
        except BaseException as exc:  # pragma: no cover - surfaced by the assert below
            errors.append(exc)

    taken: list[ProcessLocalLimiter] = []
    try:
        for limiter in held:
            limiter.__enter__()
            taken.append(limiter)
        for index, graph in enumerate(graphs):
            thread = threading.Thread(target=_target, args=(index, graph), daemon=True)
            threads.append(thread)
            thread.start()
            _wait_parked(held, len(threads))
    finally:
        for limiter in reversed(taken):
            limiter.__exit__(None, None, None)

    for thread in threads:
        thread.join(timeout=DEADLOCK_TIMEOUT)
    alive = [thread.name for thread in threads if thread.is_alive()]
    assert not alive, f"runs never finished (circular wait): {alive}"
    assert not errors, errors
    return results


class TestAcquisitionOrderIsGlobal:
    """``provider_permits`` is the single authority on acquisition order."""

    def test_order_is_the_same_whatever_scope_a_limiter_arrives_from(self):
        first = ProcessLocalLimiter(max_in_flight=1)
        second = ProcessLocalLimiter(max_in_flight=1)
        third = ProcessLocalLimiter(max_in_flight=1)

        # Same three limiters, three different scope assignments, ONE order.
        assert provider_permits((first, second), third) == (first, second, third)
        assert provider_permits((third, second), first) == (first, second, third)
        assert provider_permits((second, third), first) == (first, second, third)
        assert provider_permits((third,), first) == (first, third)
        assert provider_permits((), None) == ()

    def test_order_is_construction_rank_not_declaration_position(self):
        early = ProcessLocalLimiter(max_in_flight=1)
        late = ProcessLocalLimiter(max_in_flight=1)

        assert provider_permits((late,), early) == (early, late)
        assert provider_permits((early,), late) == (early, late)

    def test_rank_is_monotonic_for_the_life_of_the_process(self):
        limiters = [ProcessLocalLimiter(max_in_flight=1) for _ in range(5)]
        ranks = [limiter._acquisition_rank for limiter in limiters]

        assert ranks == sorted(ranks)
        assert len(set(ranks)) == len(ranks)  # never reused, unlike id()

    def test_ranks_stay_unique_when_limiters_are_built_concurrently(self):
        built: list[ProcessLocalLimiter] = []
        lock = threading.Lock()
        start = threading.Event()

        def _build() -> None:
            start.wait(DEADLOCK_TIMEOUT)
            batch = [ProcessLocalLimiter(max_in_flight=1) for _ in range(50)]
            with lock:
                built.extend(batch)

        threads = [threading.Thread(target=_build, daemon=True) for _ in range(4)]
        for thread in threads:
            thread.start()
        start.set()
        for thread in threads:
            thread.join(timeout=DEADLOCK_TIMEOUT)

        ranks = [limiter._acquisition_rank for limiter in built]
        assert len(built) == 200
        assert len(set(ranks)) == 200


class TestDedupSurvivesSorting:
    """Sorting must not turn one shared budget into a double acquire."""

    def test_the_same_limiter_at_two_scopes_still_yields_one_permit(self):
        shared = ProcessLocalLimiter(max_in_flight=1)
        other = ProcessLocalLimiter(max_in_flight=1)

        assert provider_permits((shared,), shared) == (shared,)
        assert provider_permits((shared, other), shared) == (shared, other)
        assert provider_permits((other, shared), other) == (shared, other)
        assert provider_permits((shared, shared, other), shared) == (shared, other)

    async def test_a_graph_sharing_its_budget_with_a_node_does_not_self_deadlock(self):
        shared = ProcessLocalLimiter(max_in_flight=1)
        held: list[int] = []

        @node(output_name="out", provider_limit=shared)
        async def work(x: int) -> int:
            held.append(shared.in_flight)
            return x + 1

        graph = Graph([work], name="shared").with_provider_limit(shared)
        result = await asyncio.wait_for(AsyncRunner().run(graph, {"x": 1}), timeout=DEADLOCK_TIMEOUT)

        assert result["out"] == 2
        assert held == [1]  # ONE permit, not two
        assert shared.in_flight == 0

    def test_sync_mirror_of_the_shared_budget(self):
        shared = ProcessLocalLimiter(max_in_flight=1)
        held: list[int] = []

        @node(output_name="out", provider_limit=shared)
        def work(x: int) -> int:
            held.append(shared.in_flight)
            return x + 1

        graph = Graph([work], name="shared_sync").with_provider_limit(shared)
        result = SyncRunner().run(graph, {"x": 1})

        assert result["out"] == 2
        assert held == [1]
        assert shared.in_flight == 0


class TestConflictingOrdersAcrossGraphs:
    """Two legal graphs that name the same budgets in opposite scopes."""

    async def test_two_graphs_with_opposite_scope_orders_complete(self):
        alpha = ProcessLocalLimiter(max_in_flight=1)
        beta = ProcessLocalLimiter(max_in_flight=1)

        graphs = [
            _limited_graph("ab", graph_limit=alpha, node_limit=beta),
            _limited_graph("ba", graph_limit=beta, node_limit=alpha),
        ]
        results = await _run_all_parked_then_release(graphs, (alpha, beta))

        assert results[0]["ab_out"] == 0
        assert results[1]["ba_out"] == 1
        assert alpha.in_flight == 0 and beta.in_flight == 0

    def test_sync_mirror_of_two_graphs_with_opposite_scope_orders(self):
        alpha = ProcessLocalLimiter(max_in_flight=1)
        beta = ProcessLocalLimiter(max_in_flight=1)

        graphs = [
            _limited_sync_graph("ab", graph_limit=alpha, node_limit=beta),
            _limited_sync_graph("ba", graph_limit=beta, node_limit=alpha),
        ]
        results = _run_all_parked_then_release_sync(graphs, (alpha, beta))

        assert results[0]["ab_out"] == 0
        assert results[1]["ba_out"] == 1
        assert alpha.in_flight == 0 and beta.in_flight == 0

    async def test_three_limiters_in_a_rotation_complete(self):
        alpha = ProcessLocalLimiter(max_in_flight=1)
        beta = ProcessLocalLimiter(max_in_flight=1)
        gamma = ProcessLocalLimiter(max_in_flight=1)

        graphs = [
            _limited_graph("ab", graph_limit=alpha, node_limit=beta),
            _limited_graph("bc", graph_limit=beta, node_limit=gamma),
            _limited_graph("ca", graph_limit=gamma, node_limit=alpha),
        ]
        results = await _run_all_parked_then_release(graphs, (alpha, beta, gamma))

        assert [results[0]["ab_out"], results[1]["bc_out"], results[2]["ca_out"]] == [0, 1, 2]
        assert alpha.in_flight == 0 and beta.in_flight == 0 and gamma.in_flight == 0

    def test_sync_mirror_of_the_three_limiter_rotation(self):
        alpha = ProcessLocalLimiter(max_in_flight=1)
        beta = ProcessLocalLimiter(max_in_flight=1)
        gamma = ProcessLocalLimiter(max_in_flight=1)

        graphs = [
            _limited_sync_graph("ab", graph_limit=alpha, node_limit=beta),
            _limited_sync_graph("bc", graph_limit=beta, node_limit=gamma),
            _limited_sync_graph("ca", graph_limit=gamma, node_limit=alpha),
        ]
        results = _run_all_parked_then_release_sync(graphs, (alpha, beta, gamma))

        assert [results[0]["ab_out"], results[1]["bc_out"], results[2]["ca_out"]] == [0, 1, 2]


class TestNestedGraphComposition:
    """The cycle can also be spelled across the nested-graph boundary."""

    @staticmethod
    def _nested(name: str, outer_limit, inner_limit):
        @node(output_name=f"{name}_out")
        async def work(x: int) -> int:
            return x

        inner = Graph([work], name=f"{name}_inner").with_provider_limit(inner_limit)
        return Graph([inner.as_node(name=f"{name}_nested")], name=f"{name}_outer").with_provider_limit(outer_limit)

    @staticmethod
    def _nested_sync(name: str, outer_limit, inner_limit):
        @node(output_name=f"{name}_out")
        def work(x: int) -> int:
            return x

        inner = Graph([work], name=f"{name}_inner").with_provider_limit(inner_limit)
        return Graph([inner.as_node(name=f"{name}_nested")], name=f"{name}_outer").with_provider_limit(outer_limit)

    async def test_inner_budget_over_inherited_budget_in_both_orders(self):
        alpha = ProcessLocalLimiter(max_in_flight=1)
        beta = ProcessLocalLimiter(max_in_flight=1)

        graphs = [
            self._nested("ab", outer_limit=alpha, inner_limit=beta),
            self._nested("ba", outer_limit=beta, inner_limit=alpha),
        ]
        results = await _run_all_parked_then_release(graphs, (alpha, beta))

        assert results[0]["ab_out"] == 0
        assert results[1]["ba_out"] == 1
        assert alpha.in_flight == 0 and beta.in_flight == 0

    def test_sync_mirror_of_inner_budget_over_inherited_budget(self):
        alpha = ProcessLocalLimiter(max_in_flight=1)
        beta = ProcessLocalLimiter(max_in_flight=1)

        graphs = [
            self._nested_sync("ab", outer_limit=alpha, inner_limit=beta),
            self._nested_sync("ba", outer_limit=beta, inner_limit=alpha),
        ]
        results = _run_all_parked_then_release_sync(graphs, (alpha, beta))

        assert results[0]["ab_out"] == 0
        assert results[1]["ba_out"] == 1

    async def test_a_nested_graph_that_re_declares_the_inherited_budget(self):
        """Dedup across the boundary: one permit, still no self-deadlock."""
        shared = ProcessLocalLimiter(max_in_flight=1)
        held: list[int] = []

        @node(output_name="out")
        async def work(x: int) -> int:
            held.append(shared.in_flight)
            return x + 1

        inner = Graph([work], name="inner").with_provider_limit(shared)
        outer = Graph([inner.as_node(name="nested")], name="outer").with_provider_limit(shared)

        result = await asyncio.wait_for(AsyncRunner().run(outer, {"x": 1}), timeout=DEADLOCK_TIMEOUT)

        assert result["out"] == 2
        assert held == [1]
        assert shared.in_flight == 0

    async def test_three_deep_nesting_orders_by_rank_not_by_depth(self):
        """A late-built outer budget still sorts after an early inner one."""
        inner_budget = ProcessLocalLimiter(max_in_flight=1)
        outer_budget = ProcessLocalLimiter(max_in_flight=1)  # built AFTER the inner one
        held: list[tuple[int, int]] = []

        @node(output_name="out")
        async def work(x: int) -> int:
            held.append((outer_budget.in_flight, inner_budget.in_flight))
            return x + 1

        inner = Graph([work], name="inner").with_provider_limit(inner_budget)
        outer = Graph([inner.as_node(name="nested")], name="outer").with_provider_limit(outer_budget)

        result = await asyncio.wait_for(AsyncRunner().run(outer, {"x": 1}), timeout=DEADLOCK_TIMEOUT)

        assert result["out"] == 2
        assert held == [(1, 1)]  # both budgets held, whatever the sort order
        assert outer_budget.in_flight == 0 and inner_budget.in_flight == 0


class TestFairnessIsUnchanged:
    """Ordering is a composition rule; one limiter's queue is still FIFO."""

    async def test_a_single_limiter_still_grants_in_arrival_order(self):
        limiter = ProcessLocalLimiter(max_in_flight=1)
        order: list[int] = []
        release = asyncio.Event()

        async def take(index: int) -> None:
            async with limiter:
                order.append(index)
                if index == 0:
                    await release.wait()

        async def _body() -> None:
            first = asyncio.create_task(take(0))
            while limiter.in_flight < 1:
                await asyncio.sleep(0)
            rest = []
            for index in range(1, 4):
                rest.append(asyncio.create_task(take(index)))
                while _parked(limiter) < index:
                    await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, *rest)

        await asyncio.wait_for(_body(), timeout=DEADLOCK_TIMEOUT)

        assert order == [0, 1, 2, 3]

    async def test_a_thread_is_not_starved_by_a_stream_of_tasks(self):
        limiter = ProcessLocalLimiter(max_in_flight=1)
        order: list[str] = []
        release = asyncio.Event()
        thread_done = threading.Event()

        async def take_async(tag: str) -> None:
            async with limiter:
                order.append(tag)
                if tag == "holder":
                    await release.wait()

        def take_sync() -> None:
            with limiter:
                order.append("thread")
            thread_done.set()

        async def _body() -> None:
            holder = asyncio.create_task(take_async("holder"))
            while limiter.in_flight < 1:
                await asyncio.sleep(0)
            worker = threading.Thread(target=take_sync, daemon=True)
            worker.start()
            while _parked(limiter) < 1:
                await asyncio.sleep(0.001)
            late = asyncio.create_task(take_async("late_task"))
            while _parked(limiter) < 2:
                await asyncio.sleep(0)
            release.set()
            await asyncio.gather(holder, late)
            while not thread_done.is_set():
                await asyncio.sleep(0.001)

        await asyncio.wait_for(_body(), timeout=DEADLOCK_TIMEOUT)

        assert order == ["holder", "thread", "late_task"]  # arrival order, not kind order


class TestSyncRunnerDelegatedFromAsyncRunner:
    """A sync runner inline on the loop thread cannot take a permit safely."""

    async def test_a_delegated_sync_runner_under_a_budget_is_refused(self):
        quota = ProcessLocalLimiter(max_in_flight=1)

        @node(output_name="out")
        def work(x: int) -> int:
            return x + 1

        inner = Graph([work], name="inner")
        outer = Graph([inner.as_node(name="nested", runner=SyncRunner())], name="outer").with_provider_limit(quota)

        with pytest.raises(Exception) as excinfo:  # noqa: B017 - the chain is the assertion
            await asyncio.wait_for(AsyncRunner().run(outer, {"x": 1}), timeout=DEADLOCK_TIMEOUT)

        chain: list[str] = []
        error: BaseException | None = excinfo.value
        while error is not None:
            chain.append(str(error))
            error = error.__cause__
        joined = "\n".join(chain)
        assert "provider" in joined.lower()
        assert "How to fix:" in joined
        assert quota.in_flight == 0

    async def test_a_delegated_sync_runner_without_any_budget_still_runs(self):
        """Control: the refusal is about permits, not about sync delegation."""

        @node(output_name="out")
        def work(x: int) -> int:
            return x + 1

        inner = Graph([work], name="inner")
        outer = Graph([inner.as_node(name="nested", runner=SyncRunner())], name="outer")

        result = await asyncio.wait_for(AsyncRunner().run(outer, {"x": 1}), timeout=DEADLOCK_TIMEOUT)

        assert result["out"] == 2
