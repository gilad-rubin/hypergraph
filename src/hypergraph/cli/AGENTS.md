# CLI Agent Guide

CLI output is a user-facing contract. Keep human output readable and JSON
output stable.

## Graph Inspection

- Use `graph.inputs` as the canonical source for graph-facing input shape.
  Do not revive older `graph.input_spec` assumptions.
- Human inspect output should preserve useful default information. Optional
  input names alone are not enough when bound or signature defaults are
  available.
- When changing inspect output, add coverage for both human text and `--json`
  contracts.

## Value Parsing

- Treat inline JSON and file paths as overlapping user inputs. Whitespace,
  legal filenames, and JSON-looking path names need explicit tests before
  changing detection rules.
- Error messages for parsed CLI lists should identify the invalid shape
  directly, such as empty names or empty comma-separated parts.
