"""``max_concurrency_exempt``: nodes the runner's shared limit does not gate.

``max_concurrency`` is a structural budget over in-flight work. It is the wrong
instrument for a node whose capacity is already governed by something EXTERNAL —
a provider lane, a rate limiter, a pool the node waits on itself. Such a node
does not need a runner permit, and taking one is actively harmful: a permit held
by a slow waiter is a permit the cheap, externally-bounded node cannot have, so
it queues behind work it has nothing to do with.

The shape below is deliberately order-independent. ``hog`` announces that it
owns a permit; ``free`` refuses to finish until that announcement arrives. So
whichever node the scheduler reaches first, the run settles only when ``free``
needs no permit, and hangs whenever it does. Every test is bounded, so a
regression reports a failure instead of wedging the session.
"""

import asyncio

import pytest

from hypergraph import AsyncRunner, Graph, node

# Long enough that a slow machine never trips it, short enough that a real
# regression reports in seconds.
DEADLOCK_TIMEOUT = 10.0


def _hog_and_free(*, free_timeout: float | None = None) -> tuple[Graph, dict[str, bool], asyncio.Event]:
    """One permit-hogging waiter and one node that only it can unblock.

    ``free_timeout`` puts ``free`` on the retry/timeout attempt path, which
    acquires the permit through different code than the plain path.
    """
    holds_permit = asyncio.Event()
    released = asyncio.Event()
    ran = {"free": False}

    @node(output_name="hogged")
    async def hog(x: int) -> int:
        holds_permit.set()
        await released.wait()
        return x

    @node(output_name="freed", timeout=free_timeout)
    async def free(x: int) -> int:
        await holds_permit.wait()
        ran["free"] = True
        released.set()
        return x

    return Graph([hog, free], name="hog_and_free"), ran, released


########## the falsifier pair ##########


async def test_exempt_node_runs_while_the_limit_is_fully_occupied():
    graph, ran, _released = _hog_and_free()

    result = await asyncio.wait_for(
        AsyncRunner().run(graph, {"x": 1}, max_concurrency=1, max_concurrency_exempt=["free"]),
        timeout=DEADLOCK_TIMEOUT,
    )

    assert ran["free"] is True
    assert result["hogged"] == 1
    assert result["freed"] == 1


async def test_a_non_exempt_node_waits_in_exactly_the_same_setup():
    """The control: without the exemption the same graph cannot settle. Either
    the hog owns the permit and ``free`` queues for it, or ``free`` owns it and
    parks — and then the hog never gets one to announce."""
    graph, ran, released = _hog_and_free()

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            AsyncRunner().run(graph, {"x": 1}, max_concurrency=1),
            timeout=1.0,
        )

    assert ran["free"] is False
    released.set()


async def test_a_node_with_a_timeout_is_exempt_on_the_attempt_path_too():
    """Retry/timeout nodes take the permit per ATTEMPT, through different code.
    Exemption is a property of the node, not of the path it happens to take."""
    graph, ran, _released = _hog_and_free(free_timeout=DEADLOCK_TIMEOUT)

    result = await asyncio.wait_for(
        AsyncRunner().run(graph, {"x": 1}, max_concurrency=1, max_concurrency_exempt=["free"]),
        timeout=DEADLOCK_TIMEOUT,
    )

    assert ran["free"] is True
    assert result["freed"] == 1


########## nested graphs inherit the exemption ##########


def _nested_hog_and_free() -> tuple[Graph, dict[str, bool], asyncio.Event]:
    holds_permit = asyncio.Event()
    released = asyncio.Event()
    ran = {"inner_free": False}

    @node(output_name="hogged")
    async def hog(x: int) -> int:
        holds_permit.set()
        await released.wait()
        return x

    @node(output_name="inner_freed")
    async def inner_free(x: int) -> int:
        await holds_permit.wait()
        ran["inner_free"] = True
        released.set()
        return x

    inner = Graph([inner_free], name="inner")
    return Graph([hog, inner.as_node(name="nested")], name="outer"), ran, released


async def test_exemption_reaches_a_node_inside_a_nested_graph():
    """A limit set at the outer run bounds nested nodes too, so the exemption
    has to travel with it — half an inherited limiter would re-gate the node."""
    graph, ran, _released = _nested_hog_and_free()

    result = await asyncio.wait_for(
        AsyncRunner().run(graph, {"x": 1}, max_concurrency=1, max_concurrency_exempt=["inner_free"]),
        timeout=DEADLOCK_TIMEOUT,
    )

    assert ran["inner_free"] is True
    assert result["hogged"] == 1


async def test_a_nested_node_without_the_exemption_still_takes_a_permit():
    """Control for the nested case: inheritance carries the limit itself."""
    graph, ran, released = _nested_hog_and_free()

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            AsyncRunner().run(graph, {"x": 1}, max_concurrency=1),
            timeout=1.0,
        )

    assert ran["inner_free"] is False
    released.set()


########## the exemption is opt-in and does not leak ##########


async def test_exempt_nodes_still_run_when_no_limit_is_set():
    @node(output_name="doubled")
    async def double(x: int) -> int:
        return x * 2

    result = await AsyncRunner().run(Graph([double]), {"x": 4}, max_concurrency_exempt=["double"])

    assert result["doubled"] == 8


async def test_map_installs_the_limiter_with_its_exemptions():
    """map() sets up the shared limiter for a whole batch; the exemptions have
    to be installed with it, not left behind at run()."""
    graph, ran, _released = _hog_and_free()

    await asyncio.wait_for(
        AsyncRunner().map(
            graph,
            {"x": [1]},
            map_over="x",
            max_concurrency=1,
            max_concurrency_exempt=["free"],
        ),
        timeout=DEADLOCK_TIMEOUT,
    )

    assert ran["free"] is True


########## a fan-out of exempt work is bounded by its own budget, not the runner's ##########


def _fan_out_graph(parties: int) -> tuple[Graph, dict[str, int]]:
    """A mapped inner graph whose leaf must be in flight ``parties`` times at
    once. This is the shape a per-item pipeline takes: the outer run holds the
    structural limit, and the mapped leaf's real budget lives elsewhere.
    """
    everyone_arrived = asyncio.Event()
    stats = {"current": 0, "peak": 0}

    @node(output_name="fetched")
    async def fetch(item: int) -> int:
        stats["current"] += 1
        stats["peak"] = max(stats["peak"], stats["current"])
        if stats["current"] >= parties:
            everyone_arrived.set()
        await everyone_arrived.wait()
        stats["current"] -= 1
        return item

    inner = Graph([fetch], name="fetch_one")
    return Graph([inner.as_node(name="fetch_all").map_over("item")], name="fan_out"), stats


async def test_an_exempt_leaf_fans_out_past_the_runner_limit():
    items = 6
    graph, stats = _fan_out_graph(items)

    await asyncio.wait_for(
        AsyncRunner().run(
            graph,
            {"item": list(range(items))},
            max_concurrency=2,
            max_concurrency_exempt=["fetch"],
        ),
        timeout=DEADLOCK_TIMEOUT,
    )

    assert stats["peak"] == items  # 6 at once, past a runner limit of 2


async def test_naming_an_unknown_node_leaves_the_limit_in_force():
    """A name matching no node is inert — it must not blanket-disable the gate."""
    items = 6
    graph, stats = _fan_out_graph(items)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            AsyncRunner().run(
                graph,
                {"item": list(range(items))},
                max_concurrency=2,
                max_concurrency_exempt=["not_a_node"],
            ),
            timeout=1.0,
        )

    assert stats["peak"] <= 2
