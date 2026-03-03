# Widget Preferences (Canonical)

Purpose: a single source of truth for notebook/HTML widget UX decisions.

Scope:
- `_repr_html_` widgets (`RunTable`, `MapResult`, `MapLog`, etc.)
- notebook progress UI
- related CLI parity for inspection/discovery workflows

## Core UX Defaults

1. Prefer clean summary tables with drilldown outside the table.
- Do not expand rich content inside table cells.
- Use a separate detail section below the table (`details`/master-detail pattern).

2. Default hierarchy view should be parent-first.
- In run tables, default to parent-only view.
- Child runs must be hidden by default and shown only when explicitly requested.

3. Avoid redundant information.
- If a table already shows execution steps/status, do not repeat the same records in an extra log block.
- Keep one authoritative surface per level of detail.

4. Use shared controls for filtering/sorting/pagination.
- Add control behavior in common helpers first (`src/hypergraph/_repr.py`).
- Reuse shared controls in all widgets instead of custom one-off JS.

5. Never hard-truncate results in drilldowns.
- All items must remain reachable via pagination.
- Always show an explicit `All (N)` option.

6. Use intelligent pagination defaults.
- Default page size should be readable but efficient (e.g., 50; 100 for larger sets).
- Include status filter + page controls (`Prev`/`Next`) + page info text.

7. Progress widget should be notebook-native and visually stable.
- Prefer HTML notebook renderer (not raw Rich rendering in notebooks).
- Avoid duplicate progress trees/bars for the same run.
- Keep dark mode legible: no white background blocks; consistent bright success/warn/error text.
- Keep columns aligned so bar end positions are stable regardless of timing text.

8. Keep sizing compact by default.
- One step smaller over oversized defaults (font and bar dimensions).
- Avoid full-width bars unless explicitly requested.

9. CLI parity should exist for key inspection interactions.
- Expose compact equivalents for hierarchy view, filtering, sorting, limiting, and traces.
- Keep options minimal; avoid combinatorial flag explosion.

10. Any new reusable interaction must be generalized.
- If we add filter/sort/drilldown behavior in one widget, promote it to common/shared code when practical.

## Implementation Anchors

- Shared widget helpers: `src/hypergraph/_repr.py`
- Run/step/checkpoint widgets: `src/hypergraph/checkpointers/types.py`
- Map result/log widgets: `src/hypergraph/runners/_shared/types.py`
- Notebook progress widget: `src/hypergraph/events/rich_progress.py`
- CLI inspection parity: `src/hypergraph/cli/runs.py`

## Change Discipline

- Add focused regression tests for each UX rule that changes behavior.
- Prefer additive, backward-compatible CLI changes.
- Preserve existing user flows unless they violate these preferences.
