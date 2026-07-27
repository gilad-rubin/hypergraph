"""Notebook HTML rendering for durable-host read models.

Separate from ``views.py`` for the same reason the checkpointer keeps
``presenters.py`` apart from ``types.py``: a view is a frozen record of
persisted facts, and nothing about how it looks in a notebook belongs in
that record. Every renderer here is reached only through an implicit
``_repr_html_`` that has already checked ``plain_reprs()``.
"""

from __future__ import annotations

from typing import Any


def render_batch_item_html(item: Any) -> str:
    """Render notebook HTML for one ``BatchItemView``.

    The panel leads with the item's condition and its address, because
    those are what an operator acts on: the condition says whether this
    item still needs them, and ``run_ref`` is the handle every item-scoped
    verb takes. ``started`` is shown only when the item has no outcome yet
    — once an item settles, its outcome already implies whether it ran.
    """
    from hypergraph._repr import _code, html_kv, html_panel, status_badge, theme_wrap, widget_state_key
    from hypergraph.host.views import item_condition

    kvs = [html_kv("Condition", status_badge(item_condition(item)))]
    if item.status is not None:
        kvs.append(html_kv("Run status", _code(item.status.value)))
    if item.outcome is None:
        kvs.append(html_kv("Started", "yes" if item.started else "no"))
    kvs.append(html_kv("Run", _code(item.workflow_id)))
    kvs.append(html_kv("Home", _code(item.run_ref.home)))
    body = " &nbsp;|&nbsp; ".join(kvs)
    return theme_wrap(
        html_panel(f"Batch item: {item.item_key}", body),
        state_key=widget_state_key("batch-item", item.workflow_id),
    )
