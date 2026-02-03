# Inspiration & Acknowledgments

Hypergraph stands on the shoulders of giants:

## [Hamilton](https://github.com/DAGWorks-Inc/hamilton)

Hamilton pioneered the idea that a Python function's signature *is* the graph definition — inputs become edges, outputs flow downstream. Hypergraph's automatic edge inference is a direct descendant of this insight. Hamilton's team has built something genuinely elegant, and their focus on lineage, observability, and production-grade data pipelines continues to push the ecosystem forward.

## [Pipefunc](https://github.com/pipefunc/pipefunc)

Pipefunc has been a major influence on hypergraph's design. The `@pipefunc` decorator with `output_name`, the `Pipeline` that auto-connects functions by matching names, the `.map()` operation for parallel fan-out, the rename API for adapting functions to different contexts, nested pipelines for composition, and build-time type validation — hypergraph's versions of all of these trace back to Pipefunc's clean, well-thought-out API. The "think singular, scale with map" pattern is something Pipefunc got right early on.

## [Kedro-Viz](https://github.com/kedro-org/kedro-viz)

Kedro-Viz showed what graph visualization could look like — interactive, collapsible, and hierarchical. The aspiration for hypergraph's visualization layer draws heavily from Kedro-Viz's approach to making complex pipelines understandable at a glance.
