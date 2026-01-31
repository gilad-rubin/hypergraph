# Red-Team Findings Report

**Date:** Jan 27, 2026
**Author:** Jules (Red-Team Orchestrator)

## 1. Executive Summary

A comprehensive red-team analysis of the `hypergraph` library was conducted to identify bugs, design flaws, and usability issues. The analysis involved reproducing previously reported issues and exploring new scenarios involving complex graph topologies, map operations, and input specifications.

**Key Findings:**
-   **3 Confirmed Bugs**: Including critical state corruption due to mutable defaults and a cycle termination off-by-one error.
-   **4 Design Flaws**: The runner is overly monolithic, preventing partial execution of split graphs, unreachable nodes, or intermediate value injection. Additionally, `**kwargs` support in nodes is missing.
-   **1 Security/Safety Gap**: Runtime type checking is missing, even when `strict_types=True`.

## 2. Confirmed Bugs

### B1. Mutable Default Arguments Shared Across Runs (Critical)
**Description**: When a node function uses a mutable default argument (e.g., `list=[]`), the same object instance is reused across multiple `run()` calls. This leads to state leakage between unrelated executions.
**Test**: `test_v1_mutable_defaults_shared`
**Root Cause**: The runner uses the function's default values directly without deep-copying them. Python evaluates default arguments once at definition time.
**Impact**: Non-deterministic behavior and data corruption in concurrent or sequential runs.

### B2. Cycle Termination Off-By-One
**Description**: In a cyclic graph `increment <-> check`, the loop executes one more time than expected based on the logic.
**Test**: `test_cycle_termination`
**Observation**: `assert 4 == 3` failed. The loop ran an extra iteration, incrementing the counter to 4 when it should have stopped at 3.
**Root Cause**: Likely an issue in the `SyncRunner` superstep logic or how `get_ready_nodes` interacts with gate decisions.

### B3. Runtime Inputs Not Type-Checked
**Description**: The `strict_types=True` flag only enforces type compatibility between nodes at construction time. It does not validate inputs provided to `runner.run()`.
**Test**: `test_t1_runtime_type_check`
**Impact**: Users can pass invalid types (e.g., string instead of int) which propagate through the graph, causing confusing errors downstream or silent data corruption.

## 3. Design Flaws & Limitations

### D1. Monolithic Input Validation (Split Graphs)
**Description**: The runner requires *all* inputs defined in the graph to be present, even if the graph consists of disconnected subgraphs and the user only intends to run one of them.
**Test**: `test_split_graph`
**Error**: `MissingInputError: Missing required inputs: 'in2'`
**Recommendation**: Support partial execution by determining required inputs based on the reachable subgraph from the provided inputs.

### D2. Unreachable Node Validation
**Description**: Similar to D1, if a node is unreachable from the provided inputs (and thus will never execute), the runner still demands its required inputs.
**Test**: `test_unreachable_node`
**Error**: `MissingInputError: Missing required inputs: 'y'`
**Recommendation**: Prune unreachable nodes during execution planning or relax validation to only check inputs for reachable nodes.

### D3. Inability to Override Internal Values (Intermediate Execution)
**Description**: Users cannot "jump start" a graph by providing a value for an intermediate node. The runner detects the provided value but still demands the inputs for the upstream node that would have produced it.
**Test**: `test_intermediate_value`
**Error**: `MissingInputError: Missing required inputs: 'x'`
**Recommendation**: Treat provided values as "seeds" that satisfy downstream dependencies, potentially pruning upstream nodes that are no longer needed.

### D4. Lack of **kwargs Support
**Description**: Nodes using `**kwargs` do not have their dynamic inputs detected by `InputSpec`. The graph construction logic relies on static signature inspection of named parameters.
**Test**: `test_kwargs_support`
**Error**: `MissingInputError` or incorrect validation because the graph doesn't know about the keyword arguments.
**Recommendation**: This is a hard limitation of static analysis. A `bind_dynamic_inputs` API or explicit decoration might be needed.

## 4. Verified Fixes & Non-Issues

-   **Mutex Branch-Local Consumers**: The issue `G4` (previously reported as a bug) passed the test. The validation logic correctly handles cases where mutually exclusive branches produce outputs with the same name that are consumed locally.
-   **Async Parallel Execution**: `test_parallel_async` passed, confirming that `AsyncRunner` correctly executes independent async nodes in parallel.
-   **Recursive Graphs**: Creating a graph that contains itself is practically impossible due to the immutable design (a graph copies nodes at construction). Deep nesting validation correctly detects output conflicts.
-   **Map Exception Handling**: `runner.map` correctly returns `RunResult` objects with `status=FAILED` for failed items, allowing batch processing to continue.

## 5. Recommendations

1.  **Fix Mutable Defaults**: Modify `_resolve_input` to deep-copy default values.
2.  **Implement Runtime Type Checking**: Add a validation step in `run()` to check input types against the InputSpec when `strict_types=True`.
3.  **Investigate Cycle Logic**: Debug the `SyncRunner` loop to understand the extra iteration.
4.  **Enhance Runner Flexibility**: Refactor `validate_inputs` to support partial execution and intermediate value injection. This is crucial for debugging and "human-in-the-loop" workflows.
5.  **Address Kwargs**: Update documentation to explicitly state `**kwargs` are not supported, or implement a mechanism to declare them.
