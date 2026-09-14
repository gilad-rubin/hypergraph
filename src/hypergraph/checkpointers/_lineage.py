"""The fork/retry lineage view: a run's ancestry and every descendant of it.

Reading is the store's job and drawing is this module's, so the traversal is
expressed over two callbacks rather than a connection. A backend supplies
"give me this run" and "give me the runs forked or retried from this one";
what comes back is the same tree, laid out the same way, whatever holds the
rows.
"""

from __future__ import annotations

from collections.abc import Callable

from hypergraph.checkpointers.types import LineageRow, LineageView, Run, StepTable


def lineage_parent_id(run: Run) -> str | None:
    """Return the workflow-lineage parent for fork/retry traversal."""
    return run.forked_from or run.retry_of


def build_lineage(
    workflow_id: str,
    *,
    get_run: Callable[[str], Run | None],
    get_children: Callable[[str, int], list[Run]],
    get_steps: Callable[[str], StepTable] | None,
    max_runs: int,
) -> LineageView:
    """Render git-like fork lineage for one workflow id.

    Walks up to the root ancestor, then breadth-first down every fork and
    retry, and lays the result out as indented lanes.

    Args:
        workflow_id: The run to select; it is highlighted in the result.
        get_run: Reads one run by id, ``None`` when unknown.
        get_children: Reads ``(parent_id, limit)`` → the runs forked or
            retried from that parent, oldest first.
        get_steps: Reads a run's steps, or ``None`` to omit them entirely.
        max_runs: Ceiling on how many runs the view materialises.

    Returns:
        The view, with ``steps_by_run`` populated only when ``get_steps`` was
        given.

    Raises:
        ValueError: ``workflow_id`` names no run.

    Note:
        Both walks are cycle-guarded. Lineage columns are plain ids with no
        foreign key behind them, so a restored or hand-edited database can
        present a run as its own ancestor; the traversal stops rather than
        looping forever.
    """
    selected = get_run(workflow_id)
    if selected is None:
        raise ValueError(f"Unknown workflow_id: {workflow_id!r}")

    root = selected
    seen_ancestors = {root.id}
    while True:
        parent_id = lineage_parent_id(root)
        if parent_id is None:
            break
        parent = get_run(parent_id)
        if parent is None or parent.id in seen_ancestors:
            break
        root = parent
        seen_ancestors.add(root.id)

    run_by_id: dict[str, Run] = {root.id: root}
    children_by_parent: dict[str, list[Run]] = {}
    queue: list[str] = [root.id]
    while queue and len(run_by_id) < max_runs:
        parent_id = queue.pop(0)
        children = get_children(parent_id, max_runs)
        children_by_parent[parent_id] = children
        for child in children:
            if child.id in run_by_id:
                continue
            run_by_id[child.id] = child
            if len(run_by_id) >= max_runs:
                break
            queue.append(child.id)

    rows: list[LineageRow] = [LineageRow(lane="● ", run=root, depth=0, is_selected=(root.id == workflow_id))]

    def _walk(parent_id: str, *, flags: list[bool], depth: int) -> None:
        children = children_by_parent.get(parent_id, [])
        for index, child in enumerate(children):
            has_next = index < len(children) - 1
            prefix = "".join("│  " if flag else "   " for flag in flags)
            rows.append(
                LineageRow(
                    lane=f"{prefix}{'├─ ' if has_next else '└─ '}",
                    run=child,
                    depth=depth,
                    is_selected=(child.id == workflow_id),
                )
            )
            _walk(child.id, flags=[*flags, has_next], depth=depth + 1)

    _walk(root.id, flags=[], depth=1)

    steps_by_run: dict[str, StepTable] | None = None
    if get_steps is not None:
        steps_by_run = {row.run.id: get_steps(row.run.id) for row in rows}

    return LineageView(
        rows,
        selected_run_id=workflow_id,
        root_run_id=root.id,
        steps_by_run=steps_by_run,
    )
