# Codebase Structure

**Analysis Date:** 2026-01-16

## Directory Layout

```
hypergraph/
├── src/
│   └── hypergraph/           # Main package
│       ├── __init__.py       # Public API exports
│       ├── graph.py          # Graph and InputSpec classes
│       ├── _utils.py         # Private utility functions
│       ├── _typing.py        # Type compatibility checking
│       └── nodes/            # Node type subpackage
│           ├── __init__.py   # Node exports
│           ├── base.py       # HyperNode abstract base
│           ├── function.py   # FunctionNode and @node
│           ├── graph_node.py # GraphNode for composition
│           └── _rename.py    # Rename tracking utilities
├── tests/                    # Test suite
│   ├── __init__.py
│   ├── test_graph.py         # Graph class tests
│   ├── test_graph_validation.py  # Validation-specific tests
│   ├── test_nodes_base.py    # HyperNode tests
│   ├── test_nodes_function.py    # FunctionNode tests
│   ├── test_typing.py        # Type compatibility tests
│   ├── test_utils.py         # Utility function tests
│   ├── test_integration.py   # Integration tests
│   └── unit/                 # Unit test subdirectory (currently empty/unused)
├── specs/                    # Design specifications
│   ├── reviewed/             # Approved specs (source of truth)
│   ├── not_reviewed/         # Pending specs
│   ├── references/           # Reference implementations
│   └── deprecated/           # Old specs
├── docs/                     # Documentation
│   ├── api/                  # API docs
│   ├── getting-started.md    # Quick start guide
│   └── philosophy.md         # Design philosophy
├── notebooks/                # Jupyter notebooks
├── tmp/                      # Temporary files (not committed)
│   └── references/           # Reference code (langgraph clone)
├── deprecated/               # Old/deprecated code
├── .planning/                # GSD planning files
│   ├── codebase/             # Codebase analysis docs
│   ├── phases/               # Phase plans
│   └── milestones/           # Milestone definitions
├── pyproject.toml            # Project config
├── uv.lock                   # Dependency lock file
├── README.md                 # Project readme
├── CLAUDE.md                 # AI assistant instructions
└── LICENSE                   # MIT license
```

## Directory Purposes

**`src/hypergraph/`:**
- Purpose: Main library package
- Contains: All production code
- Key files: `graph.py` (core), `__init__.py` (public API)

**`src/hypergraph/nodes/`:**
- Purpose: Node type implementations
- Contains: Base class, concrete node types, helper utilities
- Key files: `base.py` (HyperNode), `function.py` (FunctionNode), `graph_node.py` (GraphNode)

**`tests/`:**
- Purpose: Test suite
- Contains: pytest test files, organized by module
- Key files: `test_graph.py` (largest, 40k lines), `test_nodes_function.py`, `test_typing.py`

**`specs/reviewed/`:**
- Purpose: Approved design specifications
- Contains: Markdown specs for all major components
- Key files: `graph.md`, `node-types.md`, `execution-types.md`, `runners.md`

**`docs/`:**
- Purpose: User-facing documentation
- Contains: Getting started, philosophy, API docs

**`.planning/`:**
- Purpose: GSD workflow planning files
- Contains: Project state, milestones, phase plans, codebase analysis

## Key File Locations

**Entry Points:**
- `src/hypergraph/__init__.py`: Package entry, public API exports
- `src/hypergraph/graph.py`: `Graph` class constructor

**Configuration:**
- `pyproject.toml`: Project metadata, dependencies, tool config
- `.python-version`: Python version (3.12)
- `CLAUDE.md`: AI assistant instructions

**Core Logic:**
- `src/hypergraph/graph.py`: Graph structure definition
- `src/hypergraph/nodes/base.py`: HyperNode abstract base
- `src/hypergraph/nodes/function.py`: FunctionNode and @node decorator
- `src/hypergraph/nodes/graph_node.py`: GraphNode for composition
- `src/hypergraph/_typing.py`: Type compatibility checking

**Testing:**
- `tests/test_graph.py`: Graph class tests (most comprehensive)
- `tests/test_nodes_function.py`: FunctionNode tests
- `tests/test_typing.py`: Type checking tests

## Naming Conventions

**Files:**
- `snake_case.py`: All Python files
- `_prefix.py`: Private/internal modules (e.g., `_utils.py`, `_typing.py`, `_rename.py`)
- `test_*.py`: Test files matching source file names

**Directories:**
- `lowercase/`: All directories
- `_prefix` not used for directories

**Classes:**
- `PascalCase`: All classes (e.g., `Graph`, `FunctionNode`, `HyperNode`)

**Functions/Methods:**
- `snake_case`: Public functions (e.g., `ensure_tuple`, `hash_definition`)
- `_snake_case`: Private methods (e.g., `_build_graph`, `_validate`)

**Variables:**
- `snake_case`: All variables
- `UPPER_CASE`: Not used (no module-level constants)

## Where to Add New Code

**New Node Type:**
- Implementation: `src/hypergraph/nodes/<node_name>.py`
- Export: Add to `src/hypergraph/nodes/__init__.py`
- Re-export: Add to `src/hypergraph/__init__.py`
- Tests: `tests/test_nodes_<node_name>.py`

**New Feature for Graph:**
- Implementation: `src/hypergraph/graph.py`
- Tests: `tests/test_graph.py` or new `tests/test_graph_<feature>.py`

**New Utility Function:**
- Shared helpers: `src/hypergraph/_utils.py`
- Type-related: `src/hypergraph/_typing.py`
- Tests: `tests/test_utils.py` or `tests/test_typing.py`

**New Top-Level Module:**
- Implementation: `src/hypergraph/<module>.py`
- Export: Add to `src/hypergraph/__init__.py`
- Tests: `tests/test_<module>.py`

**New Design Spec:**
- Draft: `specs/not_reviewed/<topic>.md`
- After approval: Move to `specs/reviewed/<topic>.md`

## Special Directories

**`tmp/`:**
- Purpose: Temporary files and reference code
- Generated: Mixed (some manual, some generated)
- Committed: No (in .gitignore)

**`deprecated/`:**
- Purpose: Old code kept for reference
- Generated: No
- Committed: Yes, but excluded from package build

**`.planning/`:**
- Purpose: GSD workflow state and plans
- Generated: By GSD commands
- Committed: Yes

**`specs/reviewed/`:**
- Purpose: Source of truth for design decisions
- Generated: No (manually written)
- Committed: Yes
- Note: CLAUDE.md says to search here when uncertain about design

---

*Structure analysis: 2026-01-16*
