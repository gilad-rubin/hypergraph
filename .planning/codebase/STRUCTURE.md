# Codebase Structure

**Analysis Date:** 2026-01-16

## Directory Layout

```
hypergraph/
├── src/hypergraph/           # Main package source (ACTIVE)
│   ├── __init__.py           # Public API exports
│   ├── graph.py              # Graph and InputSpec classes
│   ├── _utils.py             # Shared utilities
│   └── nodes/                # Node type implementations
│       ├── __init__.py       # Node subpackage exports
│       ├── base.py           # HyperNode abstract base
│       ├── function.py       # FunctionNode and @node decorator
│       ├── graph_node.py     # GraphNode for composition
│       └── _rename.py        # Rename tracking utilities
├── tests/                    # Test suite
│   ├── __init__.py
│   ├── test_graph.py         # Graph and InputSpec tests
│   ├── test_graph_validation.py
│   ├── test_nodes_base.py    # HyperNode tests
│   ├── test_nodes_function.py # FunctionNode tests
│   ├── test_utils.py         # Utility function tests
│   └── test_integration.py   # End-to-end workflow tests
├── specs/                    # Design specifications
│   ├── reviewed/             # Approved specs (~70% refined)
│   ├── not_reviewed/         # Draft specs
│   ├── references/           # Reference implementations
│   ├── examples/             # Example code
│   └── deprecated/           # Old specs
├── .ruler/                   # Code style rules and instructions
├── .planning/                # Planning documents (GSD)
│   └── codebase/             # Codebase analysis docs
├── docs/                     # Documentation (sparse)
│   └── api/                  # API docs placeholder
├── tmp/                      # Temporary files (gitignored)
├── pyproject.toml            # Project configuration
├── CLAUDE.md                 # AI assistant instructions
└── uv.lock                   # Dependency lockfile
```

## Directory Purposes

**`src/hypergraph/`:**
- Purpose: Main package implementation (production code)
- Contains: Graph structure, node types, utilities
- Key files: `graph.py`, `nodes/function.py`, `nodes/base.py`
- Status: Active development, source of truth

**`src/hypergraph/nodes/`:**
- Purpose: All node type implementations
- Contains: Abstract base, function wrapper, graph composition
- Key files: `base.py` (HyperNode), `function.py` (FunctionNode, @node), `graph_node.py` (GraphNode)

**`tests/`:**
- Purpose: pytest test suite
- Contains: Unit tests organized by module, integration tests
- Key files: `test_graph.py` (most comprehensive), `test_integration.py`

**`specs/reviewed/`:**
- Purpose: Approved design specifications
- Contains: Markdown specs for runners, persistence, observability, etc.
- Status: ~70% refined, guides future implementation
- Key files: `graph.md`, `node-types.md`, `state-model.md`, `runners.md`

**`.ruler/`:**
- Purpose: Code style rules and AI instructions
- Contains: `code_structure.md`, `design_principles.md`, `instructions.md`
- Used by: CLAUDE.md generation via `ruler apply`

## Key File Locations

**Entry Points:**
- `src/hypergraph/__init__.py`: Public API exports
- `pyproject.toml`: Package configuration and dependencies

**Configuration:**
- `pyproject.toml`: Project metadata, dependencies, build config, pytest config
- `.ruler/`: Code style and instruction sources
- `CLAUDE.md`: Generated AI instructions

**Core Logic:**
- `src/hypergraph/graph.py`: Graph class, InputSpec, GraphConfigError
- `src/hypergraph/nodes/base.py`: HyperNode ABC, rename operations
- `src/hypergraph/nodes/function.py`: FunctionNode, @node decorator
- `src/hypergraph/nodes/graph_node.py`: GraphNode for composition

**Testing:**
- `tests/test_graph.py`: Graph construction, inputs, bind, hash, as_node
- `tests/test_integration.py`: End-to-end workflows, async, generators

## Naming Conventions

**Files:**
- `snake_case.py` for all Python modules
- Leading underscore `_name.py` for internal/private modules
- `test_*.py` for test files

**Directories:**
- `snake_case/` for packages
- No leading underscore on directories (except `.hidden`)

**Classes:**
- `PascalCase` for all classes
- Suffix `Node` for node types: `FunctionNode`, `GraphNode`, `HyperNode`
- Suffix `Error` for exceptions: `RenameError`, `GraphConfigError`

**Functions:**
- `snake_case` for all functions and methods
- Leading underscore `_private_method` for internal methods
- `with_*` prefix for immutable transformation methods

**Variables:**
- `snake_case` for variables
- `UPPER_CASE` for constants (none currently)
- Leading underscore for private attributes: `_rename_history`, `_nodes`

## Where to Add New Code

**New Node Type:**
- Implementation: `src/hypergraph/nodes/new_node.py`
- Tests: `tests/test_nodes_new_node.py`
- Export: Add to `src/hypergraph/nodes/__init__.py` and `src/hypergraph/__init__.py`
- Spec: Update `specs/reviewed/node-types.md`

**New Graph Feature:**
- Implementation: `src/hypergraph/graph.py`
- Tests: `tests/test_graph.py` or new `tests/test_graph_feature.py`
- Spec: Update `specs/reviewed/graph.md`

**New Runner (future):**
- Implementation: `src/hypergraph/runners/` (new directory)
- Tests: `tests/test_runners.py` or `tests/runners/`
- Spec: Update `specs/reviewed/runners.md` and `runners-api-reference.md`

**Utilities:**
- Shared helpers: `src/hypergraph/_utils.py`
- Node-specific helpers: `src/hypergraph/nodes/_*.py`

## Special Directories

**`specs/`:**
- Purpose: Design documentation and specifications
- Generated: No (hand-written)
- Committed: Yes
- Note: `reviewed/` is source of truth, `not_reviewed/` is drafts

**`tmp/`:**
- Purpose: Temporary files, scratch work
- Generated: Various
- Committed: No (gitignored)

**`.venv/`:**
- Purpose: Python virtual environment
- Generated: Yes (by uv/pip)
- Committed: No (gitignored)

**`.planning/`:**
- Purpose: GSD planning documents
- Generated: By GSD commands
- Committed: Varies by project preference

**`.ruler/`:**
- Purpose: Source for CLAUDE.md and AGENTS.md
- Generated: No (hand-written)
- Committed: Yes
- Note: Run `ruler apply --agents cursor,claude` after changes

---

*Structure analysis: 2026-01-16*
