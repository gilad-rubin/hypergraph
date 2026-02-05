# Inspiration & Acknowledgments

Hypergraph stands on the shoulders of giants. These frameworks shaped the Python workflow ecosystem and directly influenced hypergraph's design.

## [Hamilton](https://github.com/DAGWorks-Inc/hamilton)

Hamilton pioneered the idea that a Python function's signature *is* the graph definition — parameter names become edges, return values flow downstream. Born at Stitch Fix in 2019 for production feature engineering, Hamilton proved that this approach works at scale. Stefan Krawczyk and Elijah ben Izzy later founded DAGWorks to develop it as an open-source project.

Hamilton's strengths run deep. Its lineage tracking gives you full visibility into what depends on what. The Hamilton UI provides execution telemetry, data catalogs, and artifact inspection. Function modifiers like `@config.when()` and `@parameterize()` keep DAGs DRY without sacrificing readability. And the framework's portability — define once, run in notebooks, scripts, Airflow, or Spark — is a model for how workflow tools should work.

Hypergraph's automatic edge inference is a direct descendant of Hamilton's core insight. Where the two diverge is in scope: Hamilton is a mature, production-tested DAG framework with deep observability. Hypergraph extends the same function-as-node philosophy into cycles, conditional routing, and agentic patterns — territory that DAG frameworks don't cover by design.

## [Pipefunc](https://github.com/pipefunc/pipefunc)

Pipefunc, created by Bas Nijholt in 2023, has been a major influence on hypergraph's API. The `@pipefunc` decorator with `output_name`, the `Pipeline` that auto-connects functions by matching names, `.map()` for parallel fan-out, the rename API for adapting functions to different contexts, nested pipelines for composition, and build-time type validation — hypergraph's versions of all of these trace back to pipefunc's clean, well-thought-out design.

Pipefunc is particularly strong in scientific computing. Its `MapSpec` enables n-dimensional parameter sweeps with fine-grained parallelization, and it has first-class SLURM/HPC integration for distributing work across clusters. The framework is also remarkably lightweight — ~15 microseconds overhead per function — making it suitable for compute-intensive workloads where orchestration cost matters.

The "think singular, scale with map" pattern is something pipefunc got right early on. Hypergraph adopted this philosophy and extended it into a different domain: where pipefunc excels at scaling pure computations across parameter spaces, hypergraph adds runtime conditional routing, cycles, and human-in-the-loop patterns for interactive and agentic workflows.

## [Kedro](https://github.com/kedro-org/kedro)

Kedro brought software engineering discipline to data science. Created at QuantumBlack (McKinsey's AI arm) and open-sourced in 2019, it reached 1.0 in 2024 and graduated as a Linux Foundation project. Kedro showed that data pipelines deserve the same rigor as production software: standardized project structure, a Data Catalog that abstracts I/O across storage backends, environment-specific configuration, and modular pipelines that teams can share and compose.

Kedro's influence on the broader ecosystem is hard to overstate. It brought conventions — clear directory layouts, configuration management, hooks and plugins — to a space that was dominated by ad-hoc scripts and sprawling notebooks. For teams that need reproducibility and governance across complex data workflows, Kedro remains one of the most mature choices available.

Hypergraph draws specific inspiration from [Kedro-Viz](https://github.com/kedro-org/kedro-viz), Kedro's interactive visualization tool. Its collapsible namespace hierarchies, tag-based filtering, and ability to make large pipelines (hundreds of nodes) navigable at a glance set the standard for what graph visualization should look like. Hypergraph's visualization layer aspires to that same level of clarity, adapted for graphs that include cycles and runtime state transitions.

Where Kedro and hypergraph differ is in philosophy: Kedro provides structure and conventions for the full project lifecycle (data management, configuration, deployment). Hypergraph focuses narrowly on the graph itself — pure functions, automatic wiring, minimal ceremony — and leaves project structure to the user.
