# Integration Agent Guide

Public integration packages are opt-in facades over core Hypergraph behavior.

## Public Boundary

- Keep backend-specific user APIs under `hypergraph.integrations.<name>` instead
  of adding backend knobs to core decorators or package-root exports.
- Prefer typed option models over arbitrary dictionaries. Use dictionaries only
  when an upstream library exposes that shape directly.
- Keep implementation-heavy logic in the owning runner or subsystem; integration
  modules should mostly compose and re-export the public surface.

## Mirrors

- When changing an integration API, update examples, API docs, and runner tests
  that show the import path.
- Docs should recommend explicit imports that avoid collisions with third-party
  package names, for example `from hypergraph.integrations.daft import node as
  daft_node`.
