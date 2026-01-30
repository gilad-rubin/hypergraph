
import pytest
import asyncio
from hypergraph import Graph, node, SyncRunner, AsyncRunner, route, ifelse, END, GraphConfigError

# =============================================================================
# V1: Mutable Defaults Shared
# =============================================================================
@pytest.mark.xfail(strict=True, reason="Bug: Mutable defaults are shared across runs")
def test_v1_mutable_defaults_shared():
    """Verify that mutable default arguments are shared across runs."""

    @node(output_name="result")
    def append_to_list(item: str, container: list = []) -> list:
        container.append(item)
        return container

    graph = Graph(nodes=[append_to_list])
    runner = SyncRunner()

    # Run 1
    res1 = runner.run(graph, {"item": "A"})
    assert res1["result"] == ["A"]

    # Run 2 - Should start with empty list if not buggy
    res2 = runner.run(graph, {"item": "B"})

    # If buggy, res2["result"] will be ["A", "B"]
    # We assert the CORRECT behavior (failure means bug exists)
    assert res2["result"] == ["B"], "Mutable default was shared across runs!"

# =============================================================================
# T1: Runtime Inputs Not Type-Checked
# =============================================================================
@pytest.mark.xfail(strict=True, reason="Bug: Runtime inputs not type-checked")
def test_t1_runtime_type_check():
    """Verify that runtime inputs are type-checked when strict_types=True."""

    @node(output_name="y")
    def double(x: int) -> int:
        return x * 2

    # Enable strict_types
    graph = Graph(nodes=[double], strict_types=True)
    runner = SyncRunner()

    # Pass a string instead of int
    # If buggy, this will run and return "55" instead of raising TypeError
    try:
        res = runner.run(graph, {"x": "5"})
        # If we get here, it didn't raise TypeError
        assert False, f"Expected TypeError for invalid input type, got result: {res['y']}"
    except TypeError:
        pass # Correct behavior
    except Exception as e:
        pytest.fail(f"Caught unexpected exception: {type(e).__name__}: {e}")

# =============================================================================
# E4 & Async: AsyncRunner Usage and Execution
# =============================================================================
@pytest.mark.asyncio
async def test_e4_async_runner_usage():
    """Verify AsyncRunner.run needs await."""
    @node(output_name="out")
    async def fast_func(x: int) -> int:
        return x

    graph = Graph(nodes=[fast_func])
    runner = AsyncRunner()

    # Correct usage
    res = await runner.run(graph, {"x": 1})
    assert res["out"] == 1

    # Incorrect usage - not awaiting
    # This might return a coroutine object or fail silently/noisily depending on implementation
    coro = runner.run(graph, {"x": 1})
    assert asyncio.iscoroutine(coro), "AsyncRunner.run should return a coroutine"
    await coro # Clean up

@pytest.mark.asyncio
async def test_async_node_execution():
    """Verify basic async node execution."""
    @node(output_name="y")
    async def async_double(x: int) -> int:
        await asyncio.sleep(0.01)
        return x * 2

    graph = Graph(nodes=[async_double])
    runner = AsyncRunner()

    res = await runner.run(graph, {"x": 10})
    assert res["y"] == 20

@pytest.mark.asyncio
async def test_mixed_sync_async():
    """Verify mixing sync and async nodes."""
    @node(output_name="y")
    def sync_double(x: int) -> int:
        return x * 2

    @node(output_name="z")
    async def async_add_one(y: int) -> int:
        await asyncio.sleep(0.01)
        return y + 1

    graph = Graph(nodes=[sync_double, async_add_one])
    runner = AsyncRunner()

    res = await runner.run(graph, {"x": 5})
    assert res["z"] == 11

@pytest.mark.asyncio
async def test_parallel_async():
    """Verify parallel execution of async nodes."""
    import time

    @node(output_name="out1")
    async def sleep_1(start: float) -> float:
        await asyncio.sleep(0.1)
        return time.time()

    @node(output_name="out2")
    async def sleep_2(start: float) -> float:
        await asyncio.sleep(0.1)
        return time.time()

    graph = Graph(nodes=[sleep_1, sleep_2])
    runner = AsyncRunner()

    start_time = time.time()
    await runner.run(graph, {"start": start_time}, max_concurrency=2)
    end_time = time.time()

    duration = end_time - start_time
    # If parallel, should take ~0.1s, not ~0.2s
    assert duration < 0.18, f"Execution took {duration:.2f}s, expected parallel execution"

# =============================================================================
# G4: Mutex Branch-Local Consumers
# =============================================================================
def test_g4_mutex_local_consumer():
    """Verify mutex detection for branch-local consumers."""

    # Branch A
    @node(output_name="a_res")
    def task_a(x: int) -> int:
        return x + 1

    @node(output_name="intermediate") # Same name as branch B
    def process_a(a_res: int) -> int:
        return a_res * 2

    @node(output_name="final_a")
    def finish_a(intermediate: int) -> int:
        return intermediate

    # Branch B
    @node(output_name="b_res")
    def task_b(x: int) -> int:
        return x + 2

    @node(output_name="intermediate") # Same name as branch A
    def process_b(b_res: int) -> int:
        return b_res * 3

    @node(output_name="final_b")
    def finish_b(intermediate: int) -> int:
        return intermediate

    # Gate
    @ifelse(when_true="task_a", when_false="task_b")
    def decider(condition: bool) -> bool:
        return condition

    # The graph structure:
    # decider -> task_a -> process_a (outputs 'intermediate') -> finish_a
    #         -> task_b -> process_b (outputs 'intermediate') -> finish_b

    # 'intermediate' is produced in both branches.
    # But it is consumed ONLY within its own branch (by finish_a and finish_b).
    # This should be VALID.

    nodes = [decider, task_a, process_a, finish_a, task_b, process_b, finish_b]

    try:
        graph = Graph(nodes=nodes)
    except GraphConfigError as e:
        pytest.fail(f"GraphConfigError raised for valid mutex pattern: {e}")

# =============================================================================
# New Scenarios: Split Graphs & Unreachable Nodes
# =============================================================================
@pytest.mark.xfail(strict=True, reason="Design Flaw: Cannot run partial graph (missing inputs)")
def test_split_graph():
    """Verify behavior with disconnected subgraphs."""

    @node(output_name="res1")
    def task1(in1: int) -> int:
        return in1 + 1

    @node(output_name="res2")
    def task2(in2: int) -> int:
        return in2 + 2

    # Graph has two disconnected components
    graph = Graph(nodes=[task1, task2])
    runner = SyncRunner()

    # Try to run just one component
    # Expectation: Should work if we provide only in1?
    # Or should it fail because graph requires both inputs?
    # Current implementation likely requires ALL inputs defined in InputSpec.

    try:
        res = runner.run(graph, {"in1": 10})
        # If this works, great!
        assert res["res1"] == 11
    except Exception as e:
        pytest.fail(f"Failed to run partial graph: {e}")

@pytest.mark.xfail(strict=True, reason="Design Flaw: Unreachable nodes still require inputs")
def test_unreachable_node():
    """Verify behavior when a node is unreachable from provided inputs."""

    @node(output_name="a")
    def make_a(x: int) -> int:
        return x + 1

    @node(output_name="b")
    def make_b(y: int) -> int:
        return y + 1

    @node(output_name="c")
    def combine(a: int, b: int) -> int:
        return a + b

    graph = Graph(nodes=[make_a, make_b, combine])
    runner = SyncRunner()

    # Provide x, but not y.
    # make_b is unreachable. combine is unreachable. make_a is reachable.
    # Should this run make_a and return? Or fail because 'y' is missing?

    try:
        res = runner.run(graph, {"x": 1})
        # If partial execution is supported, we get 'a'
        assert "a" in res
    except Exception as e:
        pytest.fail(f"Failed to run with unreachable nodes: {e}")

# =============================================================================
# Cycle Termination Off-By-One
# =============================================================================
@pytest.mark.xfail(strict=True, reason="Bug: Cycle runs one extra time")
def test_cycle_termination():
    """Verify cycle termination logic isn't off-by-one."""

    @node(output_name="count")
    def increment(count: int) -> int:
        return count + 1

    @route(targets=["increment", END])
    def check(count: int) -> str:
        if count >= 3:
            return END
        return "increment"

    graph = Graph(nodes=[increment, check])
    runner = SyncRunner()

    # Start with 0.
    # Iter 1: inc(0)->1, check(1)->"increment"
    # Iter 2: inc(1)->2, check(2)->"increment"
    # Iter 3: inc(2)->3, check(3)->END
    # Result should be 3.

    res = runner.run(graph, {"count": 0})
    assert res["count"] == 3

# =============================================================================
# Intermediate Execution
# =============================================================================
@pytest.mark.xfail(strict=True, reason="Design Flaw: Cannot override internal values (missing upstream inputs)")
def test_intermediate_value():
    """Verify behavior when providing values for internal nodes."""

    @node(output_name="mid")
    def start(x: int) -> int:
        return x + 1

    @node(output_name="final")
    def end_node(mid: int) -> int:
        return mid * 2

    graph = Graph(nodes=[start, end_node])
    runner = SyncRunner()

    # Provide 'mid' directly. Should bypass 'start'.
    # If we don't provide 'x', will it fail?
    try:
        res = runner.run(graph, {"mid": 10})
        # If it runs, result should be 20
        assert res["final"] == 20
    except Exception as e:
        pytest.fail(f"Failed to run with intermediate value: {e}")

# =============================================================================
# Error Messages
# =============================================================================
def test_error_messages():
    """Verify error message quality."""

    @node(output_name="y")
    def func(x: int) -> int:
        return x

    graph = Graph(nodes=[func])
    runner = SyncRunner()

    # 1. Rename non-existent input (bind)
    try:
        graph.bind(z=1)
    except ValueError as e:
        assert "not a graph input" in str(e)
    except Exception as e:
        pytest.fail(f"bind(z=1) raised unexpected {type(e)}: {e}")

    # 2. Missing input
    try:
        runner.run(graph, {})
    except Exception as e:
        assert "Missing required inputs" in str(e)
        assert "'x'" in str(e)

# =============================================================================
# Deep Nesting
# =============================================================================
def test_deep_nesting():
    """Verify deep nesting of graphs."""

    @node(output_name="v1")
    def n1(x: int) -> int: return x + 1
    g1 = Graph(nodes=[n1], name="g1")

    @node(output_name="v2")
    def n2(v1: int) -> int: return v1 + 1
    g2 = Graph(nodes=[g1.as_node(), n2], name="g2")

    @node(output_name="v3")
    def n3(v2: int) -> int: return v2 + 1
    g3 = Graph(nodes=[g2.as_node(), n3], name="g3")

    runner = SyncRunner()
    res = runner.run(g3, {"x": 1})
    assert res["v3"] == 4
