"""Benchmarks for optimization PRs #19, #20, #21, #22, #24.

Run with: uv run python benchmarks/test_optimization_prs.py
"""

import asyncio
import statistics
import time
import tracemalloc
from collections import Counter, defaultdict
from typing import Any

import networkx as nx


# ---------------------------------------------------------------------------
# PR #20: Exclusive reachability — O(N²) set-diff vs O(N) Counter
# ---------------------------------------------------------------------------

def _build_branch_graph(num_branches: int, depth: int) -> tuple[nx.DiGraph, list[str]]:
    """Build a fan-out graph: gate -> branch_0..branch_N, each with `depth` descendants."""
    G = nx.DiGraph()
    targets = []
    for b in range(num_branches):
        parent = f"branch_{b}"
        targets.append(parent)
        G.add_node(parent)
        for d in range(depth):
            child = f"branch_{b}_d{d}"
            G.add_edge(parent if d == 0 else f"branch_{b}_d{d-1}", child)
    return G, targets


def _exclusive_reachability_old(G: nx.DiGraph, targets: list[str]) -> dict[str, set[str]]:
    """Original O(T*N) implementation."""
    reachable = {t: set(nx.descendants(G, t)) | {t} for t in targets}
    exclusive = {}
    for t in targets:
        others = set().union(*(reachable[o] for o in targets if o != t))
        exclusive[t] = reachable[t] - others
    return exclusive


def _exclusive_reachability_new(G: nx.DiGraph, targets: list[str]) -> dict[str, set[str]]:
    """Optimized O(N) Counter-based implementation (PR #20)."""
    reachable = {t: set(nx.descendants(G, t)) | {t} for t in targets}
    # Flatten all reachable nodes and count occurrences
    all_nodes = []
    for nodes in reachable.values():
        all_nodes.extend(nodes)
    counts = Counter(all_nodes)
    # Nodes with count==1 are exclusive to one target
    exclusive = {}
    for t in targets:
        exclusive[t] = {n for n in reachable[t] if counts[n] == 1}
    return exclusive


def bench_pr20():
    """Benchmark PR #20: exclusive reachability."""
    print("\n" + "=" * 60)
    print("PR #20: Exclusive Reachability (set-diff vs Counter)")
    print("=" * 60)

    configs = [
        (5, 10),
        (10, 20),
        (20, 30),
        (50, 20),
        (100, 10),
    ]
    iterations = 20

    for num_branches, depth in configs:
        G, targets = _build_branch_graph(num_branches, depth)
        total_nodes = G.number_of_nodes()

        # Verify correctness
        old_result = _exclusive_reachability_old(G, targets)
        new_result = _exclusive_reachability_new(G, targets)
        assert old_result == new_result, f"Results differ for {num_branches}x{depth}"

        # Benchmark old
        times_old = []
        for _ in range(iterations):
            start = time.perf_counter()
            _exclusive_reachability_old(G, targets)
            times_old.append(time.perf_counter() - start)

        # Benchmark new
        times_new = []
        for _ in range(iterations):
            start = time.perf_counter()
            _exclusive_reachability_new(G, targets)
            times_new.append(time.perf_counter() - start)

        median_old = statistics.median(times_old)
        median_new = statistics.median(times_new)
        speedup = (median_old - median_new) / median_old * 100 if median_old > 0 else 0

        print(f"  branches={num_branches:3d}, depth={depth:2d}, nodes={total_nodes:5d} | "
              f"old={median_old*1000:.3f}ms, new={median_new*1000:.3f}ms | "
              f"{'speedup' if speedup > 0 else 'slowdown'}: {abs(speedup):.1f}%")


# ---------------------------------------------------------------------------
# PR #22: AsyncRunner.map — all-tasks-upfront vs worker-pool
# ---------------------------------------------------------------------------

async def _map_all_tasks(work_fn, items, max_concurrency):
    """Original: create all tasks upfront, use semaphore to limit concurrency."""
    semaphore = asyncio.Semaphore(max_concurrency)

    async def limited(item):
        async with semaphore:
            return await work_fn(item)

    tasks = [limited(item) for item in items]
    return await asyncio.gather(*tasks)


async def _map_worker_pool(work_fn, items, max_concurrency):
    """Optimized: fixed worker pool pulling from queue (PR #22)."""
    queue: asyncio.Queue = asyncio.Queue()
    for i, item in enumerate(items):
        queue.put_nowait((i, item))

    results = [None] * len(items)
    num_workers = min(max_concurrency, len(items))

    async def worker():
        while not queue.empty():
            try:
                idx, item = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            results[idx] = await work_fn(item)

    workers = [asyncio.create_task(worker()) for _ in range(num_workers)]
    await asyncio.gather(*workers)
    return results


async def _dummy_work(item):
    """Simulate lightweight async work."""
    await asyncio.sleep(0)
    return item * 2


def bench_pr22():
    """Benchmark PR #22: AsyncRunner.map worker pool."""
    print("\n" + "=" * 60)
    print("PR #22: AsyncRunner.map (all-tasks vs worker-pool)")
    print("=" * 60)

    configs = [
        (100, 10),
        (1000, 10),
        (5000, 50),
        (10000, 50),
    ]
    iterations = 5

    async def run_benchmarks():
        for num_items, max_conc in configs:
            items = list(range(num_items))

            # Verify correctness
            old_result = await _map_all_tasks(_dummy_work, items, max_conc)
            new_result = await _map_worker_pool(_dummy_work, items, max_conc)
            assert old_result == new_result, f"Results differ for {num_items}"

            # Benchmark old — memory
            tracemalloc.start()
            snap_before = tracemalloc.take_snapshot()
            await _map_all_tasks(_dummy_work, items, max_conc)
            snap_after = tracemalloc.take_snapshot()
            mem_old = sum(s.size for s in snap_after.compare_to(snap_before, 'lineno') if s.size_diff > 0)
            tracemalloc.stop()

            # Benchmark new — memory
            tracemalloc.start()
            snap_before = tracemalloc.take_snapshot()
            await _map_worker_pool(_dummy_work, items, max_conc)
            snap_after = tracemalloc.take_snapshot()
            mem_new = sum(s.size for s in snap_after.compare_to(snap_before, 'lineno') if s.size_diff > 0)
            tracemalloc.stop()

            # Benchmark old — time
            times_old = []
            for _ in range(iterations):
                start = time.perf_counter()
                await _map_all_tasks(_dummy_work, items, max_conc)
                times_old.append(time.perf_counter() - start)

            # Benchmark new — time
            times_new = []
            for _ in range(iterations):
                start = time.perf_counter()
                await _map_worker_pool(_dummy_work, items, max_conc)
                times_new.append(time.perf_counter() - start)

            median_old = statistics.median(times_old)
            median_new = statistics.median(times_new)
            time_diff = (median_old - median_new) / median_old * 100 if median_old > 0 else 0
            mem_diff = (mem_old - mem_new) / mem_old * 100 if mem_old > 0 else 0

            print(f"  items={num_items:5d}, conc={max_conc:3d} | "
                  f"time: old={median_old*1000:.1f}ms new={median_new*1000:.1f}ms ({time_diff:+.1f}%) | "
                  f"mem: old={mem_old//1024}KB new={mem_new//1024}KB ({mem_diff:+.1f}%)")

    asyncio.run(run_benchmarks())


# ---------------------------------------------------------------------------
# PR #24: controlled_by map — per-superstep vs cached
# ---------------------------------------------------------------------------

class _FakeGateNode:
    def __init__(self, name: str, targets: list[str]):
        self.name = name
        self.targets = targets


class _FakeNode:
    def __init__(self, name: str):
        self.name = name


def _build_gate_graph(num_gates: int, targets_per_gate: int, extra_nodes: int):
    """Build a dict of nodes simulating a graph with gates."""
    nodes = {}
    sentinel = object()  # stand-in for END
    for i in range(num_gates):
        gate_name = f"gate_{i}"
        gate_targets = [f"target_{i}_{j}" for j in range(targets_per_gate)]
        nodes[gate_name] = _FakeGateNode(gate_name, gate_targets + [sentinel])
        for t in gate_targets:
            nodes[t] = _FakeNode(t)
    for i in range(extra_nodes):
        nodes[f"node_{i}"] = _FakeNode(f"node_{i}")
    return nodes, sentinel


def _build_controlled_by_per_call(nodes: dict, sentinel: object) -> dict[str, list[str]]:
    """Original: rebuild controlled_by every superstep."""
    controlled_by: dict[str, list[str]] = {}
    for node in nodes.values():
        if isinstance(node, _FakeGateNode):
            for target in node.targets:
                if target is not sentinel and target in nodes:
                    controlled_by.setdefault(target, []).append(node.name)
    return controlled_by


def bench_pr24():
    """Benchmark PR #24: cached controlled_by map."""
    print("\n" + "=" * 60)
    print("PR #24: controlled_by map (per-superstep vs cached)")
    print("=" * 60)

    configs = [
        (5, 3, 20),      # small graph
        (10, 5, 50),     # medium
        (20, 5, 100),    # larger
        (50, 5, 200),    # big
    ]
    supersteps = 100
    iterations = 20

    for num_gates, tpg, extra in configs:
        nodes, sentinel = _build_gate_graph(num_gates, tpg, extra)
        total = len(nodes)

        # Verify correctness
        result = _build_controlled_by_per_call(nodes, sentinel)

        # Benchmark: rebuild every superstep
        times_old = []
        for _ in range(iterations):
            start = time.perf_counter()
            for _ in range(supersteps):
                _build_controlled_by_per_call(nodes, sentinel)
            times_old.append(time.perf_counter() - start)

        # Benchmark: build once (cached)
        times_new = []
        for _ in range(iterations):
            start = time.perf_counter()
            cached = _build_controlled_by_per_call(nodes, sentinel)
            for _ in range(supersteps):
                _ = cached  # lookup only
            times_new.append(time.perf_counter() - start)

        median_old = statistics.median(times_old)
        median_new = statistics.median(times_new)
        speedup = (median_old - median_new) / median_old * 100 if median_old > 0 else 0

        print(f"  gates={num_gates:2d}, targets/gate={tpg}, extras={extra:3d}, total={total:4d} | "
              f"rebuild×{supersteps}: {median_old*1000:.3f}ms, cached: {median_new*1000:.3f}ms | "
              f"speedup: {speedup:.1f}%")


# ---------------------------------------------------------------------------
# PR #19 & #21: Sanity checks at realistic scales
# ---------------------------------------------------------------------------

def bench_pr19_sanity():
    """PR #19: single-pass vs two-comprehension at realistic scales."""
    print("\n" + "=" * 60)
    print("PR #19: Defaults consistency check (realistic scales)")
    print("=" * 60)

    for size in [5, 10, 20, 50]:
        info_list = [(i % 2 == 0, i, f"node_{i}") for i in range(size)]
        iterations = 50_000

        # Two-comprehension (original)
        start = time.perf_counter()
        for _ in range(iterations):
            with_default = [(v, n) for has, v, n in info_list if has]
            without_default = [n for has, v, n in info_list if not has]
        time_old = time.perf_counter() - start

        # Single-loop (PR #19)
        start = time.perf_counter()
        for _ in range(iterations):
            with_default = []
            without_default = []
            for has, v, n in info_list:
                if has:
                    with_default.append((v, n))
                else:
                    without_default.append(n)
        time_new = time.perf_counter() - start

        diff = (time_old - time_new) / time_old * 100 if time_old > 0 else 0
        print(f"  size={size:3d} | two-comp: {time_old*1000:.1f}ms, single-loop: {time_new*1000:.1f}ms | "
              f"diff: {diff:+.1f}% (over {iterations} iterations)")


def bench_pr21_sanity():
    """PR #21: setdefault vs defaultdict at realistic scales."""
    print("\n" + "=" * 60)
    print("PR #21: setdefault vs defaultdict (realistic scales)")
    print("=" * 60)

    for num_nodes, outputs_per_node in [(5, 2), (10, 3), (20, 5)]:
        items = [(f"node_{i}", f"output_{j}") for i in range(num_nodes) for j in range(outputs_per_node)]
        iterations = 50_000

        # setdefault (original)
        start = time.perf_counter()
        for _ in range(iterations):
            d: dict[str, list[str]] = {}
            for node_name, output in items:
                d.setdefault(output, []).append(node_name)
        time_old = time.perf_counter() - start

        # defaultdict (PR #21)
        start = time.perf_counter()
        for _ in range(iterations):
            d2: dict[str, list[str]] = defaultdict(list)
            for node_name, output in items:
                d2[output].append(node_name)
        time_new = time.perf_counter() - start

        diff = (time_old - time_new) / time_old * 100 if time_old > 0 else 0
        print(f"  nodes={num_nodes:2d}, outputs/node={outputs_per_node} | "
              f"setdefault: {time_old*1000:.1f}ms, defaultdict: {time_new*1000:.1f}ms | "
              f"diff: {diff:+.1f}% (over {iterations} iterations)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Optimization PR Benchmarks")
    print("=" * 60)

    bench_pr20()
    bench_pr22()
    bench_pr24()
    bench_pr19_sanity()
    bench_pr21_sanity()

    print("\n" + "=" * 60)
    print("Done.")
