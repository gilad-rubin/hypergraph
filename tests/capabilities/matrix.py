"""
Capability matrix defining all feature dimensions and valid combinations.

Each dimension represents an orthogonal feature that can be combined with others.
Constraints define which combinations are valid/invalid.

Usage:
    from tests.capabilities import all_valid_combinations, Capability

    for cap in all_valid_combinations():
        graph = build_graph_for_capability(cap)
        # ... test
"""

from dataclasses import dataclass
from enum import Enum, auto
from typing import Iterator
import itertools


# =============================================================================
# Dimension Enums
# =============================================================================


class Runner(Enum):
    """Execution runner type."""

    SYNC = auto()
    ASYNC = auto()


class NodeType(Enum):
    """Types of nodes that can be in a graph."""

    SYNC_FUNC = auto()  # def foo(): ...
    ASYNC_FUNC = auto()  # async def foo(): ...
    SYNC_GENERATOR = auto()  # def foo(): yield ...
    ASYNC_GENERATOR = auto()  # async def foo(): yield ...
    GRAPH_NODE = auto()  # Nested graph


class Topology(Enum):
    """Graph structure topology."""

    LINEAR = auto()  # A -> B -> C
    BRANCHING = auto()  # A -> B, A -> C (fan-out)
    CONVERGING = auto()  # A -> C, B -> C (fan-in)
    DIAMOND = auto()  # A -> B -> D, A -> C -> D
    CYCLIC = auto()  # Contains feedback loop


class MapMode(Enum):
    """How mapping is applied (if at all)."""

    NONE = auto()  # No mapping
    ZIP = auto()  # Parallel iteration (equal lengths)
    PRODUCT = auto()  # Cartesian product


class NestingDepth(Enum):
    """How deeply graphs are nested."""

    FLAT = 0  # No nesting, just FunctionNodes
    ONE_LEVEL = 1  # One GraphNode containing FunctionNodes
    TWO_LEVELS = 2  # GraphNode containing GraphNode
    THREE_PLUS = 3  # 3+ levels of nesting


class Concurrency(Enum):
    """Concurrency control setting."""

    UNLIMITED = auto()  # No limit
    LIMITED = auto()  # max_concurrency specified


class TypeValidation(Enum):
    """Type validation mode."""

    OFF = auto()  # strict_types=False
    STRICT = auto()  # strict_types=True


class Renaming(Enum):
    """Whether nodes/inputs/outputs are renamed."""

    NONE = auto()  # No renaming applied
    INPUTS = auto()  # with_inputs() applied
    OUTPUTS = auto()  # with_outputs() applied
    NODE_NAME = auto()  # with_name() applied


class Binding(Enum):
    """Whether graph has bound values."""

    NONE = auto()  # No binding
    BOUND = auto()  # graph.bind() applied


class Caching(Enum):
    """Whether caching is enabled."""

    NONE = auto()  # No cache backend
    IN_MEMORY = auto()  # InMemoryCache on runner


class OutputConflict(Enum):
    """How duplicate output names are resolved (if at all)."""

    NONE = auto()  # No duplicate outputs
    MUTEX = auto()  # Duplicate outputs behind exclusive gate
    ORDERED = auto()  # Duplicate outputs with emit/wait_for ordering


# =============================================================================
# Capability dataclass
# =============================================================================


@dataclass(frozen=True)
class Capability:
    """
    A specific combination of feature capabilities.

    This represents one point in the N-dimensional capability space.
    Use is_valid() to check if this combination is allowed.
    """

    runner: Runner
    node_types: frozenset[NodeType]
    topology: Topology
    map_mode: MapMode
    nesting: NestingDepth
    concurrency: Concurrency
    type_validation: TypeValidation
    renaming: Renaming
    binding: Binding
    caching: Caching
    output_conflict: OutputConflict

    def is_valid(self) -> bool:
        """Check if this capability combination is valid."""
        return all(check(self) for check in _CONSTRAINT_CHECKS)

    @property
    def requires_seed(self) -> bool:
        """Whether this capability requires seed inputs."""
        return self.topology == Topology.CYCLIC

    @property
    def has_async_nodes(self) -> bool:
        """Whether this capability includes async nodes."""
        return bool(
            self.node_types & {NodeType.ASYNC_FUNC, NodeType.ASYNC_GENERATOR}
        )

    @property
    def has_nesting(self) -> bool:
        """Whether this capability includes nested graphs."""
        return self.nesting != NestingDepth.FLAT

    def __str__(self) -> str:
        """Human-readable representation for test IDs."""
        parts = [
            self.runner.name.lower(),
            "+".join(sorted(n.name.lower() for n in self.node_types)),
            self.topology.name.lower(),
        ]
        if self.map_mode != MapMode.NONE:
            parts.append(f"map_{self.map_mode.name.lower()}")
        if self.nesting != NestingDepth.FLAT:
            parts.append(f"nest_{self.nesting.value}")
        if self.concurrency == Concurrency.LIMITED:
            parts.append("limited")
        if self.type_validation == TypeValidation.STRICT:
            parts.append("strict")
        if self.renaming != Renaming.NONE:
            parts.append(f"rename_{self.renaming.name.lower()}")
        if self.binding != Binding.NONE:
            parts.append("bound")
        if self.caching != Caching.NONE:
            parts.append("cached")
        if self.output_conflict != OutputConflict.NONE:
            parts.append(f"conflict_{self.output_conflict.name.lower()}")
        return "-".join(parts)


# =============================================================================
# Constraint checks
# =============================================================================


def _sync_runner_no_async_nodes(cap: Capability) -> bool:
    """SyncRunner cannot execute async nodes."""
    if cap.runner == Runner.SYNC and cap.has_async_nodes:
        return False
    return True


def _concurrency_only_for_async(cap: Capability) -> bool:
    """Concurrency limits only make sense for async runner."""
    if cap.runner == Runner.SYNC and cap.concurrency == Concurrency.LIMITED:
        return False
    return True


def _map_requires_nesting_or_runner_map(cap: Capability) -> bool:
    """
    Map mode requires either:
    - Nesting (GraphNode.map_over)
    - Or we're testing runner.map() which works on flat graphs too

    For now, we allow map on flat graphs (runner.map case).
    """
    return True


def _graph_node_requires_nesting(cap: Capability) -> bool:
    """GraphNode type requires nesting depth > 0."""
    if NodeType.GRAPH_NODE in cap.node_types and cap.nesting == NestingDepth.FLAT:
        return False
    return True


def _nesting_requires_graph_node(cap: Capability) -> bool:
    """Nesting > 0 implies we have GraphNodes."""
    if cap.nesting != NestingDepth.FLAT and NodeType.GRAPH_NODE not in cap.node_types:
        return False
    return True


def _output_conflict_requires_topology(cap: Capability) -> bool:
    """ORDERED requires CYCLIC; MUTEX requires BRANCHING or DIAMOND."""
    if cap.output_conflict == OutputConflict.ORDERED:
        return cap.topology == Topology.CYCLIC
    if cap.output_conflict == OutputConflict.MUTEX:
        return cap.topology in {Topology.BRANCHING, Topology.DIAMOND}
    return True


_CONSTRAINT_CHECKS = [
    _sync_runner_no_async_nodes,
    _concurrency_only_for_async,
    _graph_node_requires_nesting,
    _nesting_requires_graph_node,
    _output_conflict_requires_topology,
]


# =============================================================================
# Combination generators
# =============================================================================


def _all_node_type_combinations() -> Iterator[frozenset[NodeType]]:
    """
    Generate meaningful node type combinations.

    Not all 2^5 combinations make sense. We generate:
    - Single types
    - Common mixed patterns
    """
    # Single types
    yield frozenset([NodeType.SYNC_FUNC])
    yield frozenset([NodeType.ASYNC_FUNC])
    yield frozenset([NodeType.SYNC_GENERATOR])
    yield frozenset([NodeType.ASYNC_GENERATOR])

    # Mixed sync/async (common case)
    yield frozenset([NodeType.SYNC_FUNC, NodeType.ASYNC_FUNC])

    # With GraphNode
    yield frozenset([NodeType.SYNC_FUNC, NodeType.GRAPH_NODE])
    yield frozenset([NodeType.ASYNC_FUNC, NodeType.GRAPH_NODE])
    yield frozenset([NodeType.SYNC_FUNC, NodeType.ASYNC_FUNC, NodeType.GRAPH_NODE])


def all_valid_combinations() -> Iterator[Capability]:
    """
    Generate all valid capability combinations.

    This is the main entry point for test parametrization.
    """
    for (
        runner,
        node_types,
        topology,
        map_mode,
        nesting,
        concurrency,
        type_validation,
        renaming,
        binding,
        caching,
        output_conflict,
    ) in itertools.product(
        Runner,
        _all_node_type_combinations(),
        Topology,
        MapMode,
        NestingDepth,
        Concurrency,
        TypeValidation,
        Renaming,
        Binding,
        Caching,
        OutputConflict,
    ):
        cap = Capability(
            runner=runner,
            node_types=node_types,
            topology=topology,
            map_mode=map_mode,
            nesting=nesting,
            concurrency=concurrency,
            type_validation=type_validation,
            renaming=renaming,
            binding=binding,
            caching=caching,
            output_conflict=output_conflict,
        )
        if cap.is_valid():
            yield cap


def pairwise_combinations() -> Iterator[Capability]:
    """
    Generate pairwise (2-way) combinations for efficient testing.

    Uses allpairspy to generate a minimal set of combinations where every
    pair of dimension values appears at least once. This typically reduces
    ~8000 combinations to ~100 while catching most interaction bugs.

    Returns:
        Iterator of valid Capability combinations covering all pairs
    """
    from allpairspy import AllPairs

    # Define all dimension values
    parameters = [
        list(Runner),
        list(_all_node_type_combinations()),
        list(Topology),
        list(MapMode),
        list(NestingDepth),
        list(Concurrency),
        list(TypeValidation),
        list(Renaming),
        list(Binding),
        list(Caching),
        list(OutputConflict),
    ]

    def is_valid_combo(row: list) -> bool:
        """Filter function for AllPairs - check if partial combo is valid."""
        if len(row) < 2:
            return True

        # Build partial capability to check constraints
        runner = row[0]
        node_types = row[1] if len(row) > 1 else frozenset([NodeType.SYNC_FUNC])

        # Check sync runner + async nodes constraint
        async_nodes = {NodeType.ASYNC_FUNC, NodeType.ASYNC_GENERATOR}
        if runner == Runner.SYNC and (node_types & async_nodes):
            return False

        # Check concurrency constraint (only if we have that many values)
        if len(row) > 5:
            concurrency = row[5]
            if runner == Runner.SYNC and concurrency == Concurrency.LIMITED:
                return False

        # Check nesting/graph_node constraints
        if len(row) > 4:
            nesting = row[4]
            if nesting != NestingDepth.FLAT and NodeType.GRAPH_NODE not in node_types:
                return False
            if nesting == NestingDepth.FLAT and NodeType.GRAPH_NODE in node_types:
                return False

        # Check output_conflict/topology constraint
        if len(row) > 10:
            topology = row[2]
            output_conflict = row[10]
            if output_conflict == OutputConflict.ORDERED:
                if topology != Topology.CYCLIC:
                    return False
            if output_conflict == OutputConflict.MUTEX:
                if topology not in {Topology.BRANCHING, Topology.DIAMOND}:
                    return False

        return True

    for combo in AllPairs(parameters, filter_func=is_valid_combo):
        cap = Capability(
            runner=combo[0],
            node_types=combo[1],
            topology=combo[2],
            map_mode=combo[3],
            nesting=combo[4],
            concurrency=combo[5],
            type_validation=combo[6],
            renaming=combo[7],
            binding=combo[8],
            caching=combo[9],
            output_conflict=combo[10],
        )
        # Final validation (in case filter missed something)
        if cap.is_valid():
            yield cap


def combinations_for(**filters) -> Iterator[Capability]:
    """
    Generate valid combinations matching specific filters.

    Example:
        # All async runner combinations with cyclic topology
        for cap in combinations_for(runner=Runner.ASYNC, topology=Topology.CYCLIC):
            ...

        # All combinations with map_over
        for cap in combinations_for(map_mode=MapMode.ZIP):
            ...
    """
    for cap in all_valid_combinations():
        matches = all(getattr(cap, key) == value for key, value in filters.items())
        if matches:
            yield cap


def count_combinations() -> dict[str, int]:
    """Count combinations for reporting."""
    all_combos = list(all_valid_combinations())
    return {
        "total_valid": len(all_combos),
        "by_runner": {
            "sync": sum(1 for c in all_combos if c.runner == Runner.SYNC),
            "async": sum(1 for c in all_combos if c.runner == Runner.ASYNC),
        },
        "by_topology": {
            t.name: sum(1 for c in all_combos if c.topology == t) for t in Topology
        },
        "by_nesting": {
            n.name: sum(1 for c in all_combos if c.nesting == n) for n in NestingDepth
        },
    }


if __name__ == "__main__":
    # Quick sanity check
    counts = count_combinations()
    print(f"Total valid combinations: {counts['total_valid']}")
    print(f"By runner: {counts['by_runner']}")
    print(f"By topology: {counts['by_topology']}")
    print(f"By nesting: {counts['by_nesting']}")

    print("\nFirst 10 combinations:")
    for i, cap in enumerate(all_valid_combinations()):
        if i >= 10:
            break
        print(f"  {cap}")
