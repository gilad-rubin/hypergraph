# hypergraph Examples

Design validation examples ported from LangGraph tutorials.

These examples demonstrate how hypergraph API patterns express common agent workflows, helping validate our design before implementation.

## Examples

| #  | Name                                                                     | Source                                                                                                      | Patterns                                                       |
| -- | ------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| 01 | [Calculator Agent](../../../specs/examples/01-calculator-agent.md)       | [LangGraph Quickstart](https://docs.langchain.com/oss/python/langgraph/quickstart)                          | Cycles, `@branch`, accumulator nodes                           |
| 02 | [Prompt Chaining](../../../specs/examples/02-prompt-chaining.md)         | [LangGraph Workflows](https://docs.langchain.com/oss/python/langgraph/workflows-agents#prompt-chaining)     | Sequential chain, early exit with `@branch`                    |
| 03 | [Parallelization](../../../specs/examples/03-parallelization.md)         | [LangGraph Workflows](https://docs.langchain.com/oss/python/langgraph/workflows-agents#parallelization)     | Implicit fan-out/fan-in, no explicit edges                     |
| 04 | [Routing](../../../specs/examples/04-routing.md)                         | [LangGraph Workflows](https://docs.langchain.com/oss/python/langgraph/workflows-agents#routing)             | `@route` for multi-way branching, mutually exclusive producers |
| 05 | [Orchestrator-Worker](../../../specs/examples/05-orchestrator-worker.md) | [LangGraph Workflows](https://docs.langchain.com/oss/python/langgraph/workflows-agents#orchestrator-worker) | Dynamic parallelism, `.as_node().with_inputs().map_over()`     |
| 06 | [Evaluator-Optimizer](../../../specs/examples/06-evaluator-optimizer.md) | [LangGraph Workflows](https://docs.langchain.com/oss/python/langgraph/workflows-agents#evaluator-optimizer) | Feedback loop, `.bind()` for cycle initialization              |
| 07 | [Agents](../../../specs/examples/07-agents.md)                           | [LangGraph Workflows](https://docs.langchain.com/oss/python/langgraph/workflows-agents#agents)              | Tool-calling loop, accumulator pattern (see also 01)           |
| 08 | [Subgraphs](../../../specs/examples/08-subgraphs.md)                     | [LangGraph Subgraphs](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)                        | `.as_node()`, `.with_inputs()`, `.with_outputs()`, multi-agent |

## Purpose

These examples serve to:

1. **Validate API design** - Ensure hypergraph can elegantly express real-world patterns
2. **Identify gaps** - Find missing features or awkward APIs before implementation
3. **Document patterns** - Show idiomatic hypergraph solutions
4. **Compare with LangGraph** - Highlight differences in approach

## Key Differences from LangGraph

| Aspect           | LangGraph                                    | hypergraph                            |
| ---------------- | -------------------------------------------- | ------------------------------------- |
| **State**        | Explicit `TypedDict` with annotations        | No state object - values flow by name |
| **Edges**        | `add_edge("a", "b")`                         | Implicit from parameter matching      |
| **Accumulation** | `Annotated[list, operator.add]`              | Accumulator node outputs same name    |
| **Routing**      | `add_conditional_edges(source, fn, mapping)` | `@branch` / `@route` decorators       |
| **Graph**        | `StateGraph(State).compile()`                | `Graph(nodes=[...])`                  |
| **Execution**    | `graph.invoke(inputs)`                       | `runner.run(graph, inputs)`           |
