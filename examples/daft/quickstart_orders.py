"""Daft quickstart-style batch processing expressed with Hypergraph."""

from __future__ import annotations

from hypergraph import DaftRunner, Graph, node


@node(output_name="normalized_order")
def normalize_order(order: dict) -> dict:
    return {
        "order_id": order["order_id"],
        "customer": order["customer"].strip().title(),
        "country": order["country"].upper(),
        "unit_price": float(order["unit_price"]),
        "quantity": int(order["quantity"]),
        "rush": bool(order.get("rush", False)),
    }


@node(output_name="order_total")
def compute_total(normalized_order: dict) -> float:
    return round(normalized_order["unit_price"] * normalized_order["quantity"], 2)


@node(output_name="review_queue")
def assign_queue(order_total: float, normalized_order: dict) -> str:
    if normalized_order["rush"]:
        return "priority"
    if order_total >= 100:
        return "manual_review"
    return "auto_fulfillment"


@node(output_name="fulfillment_record")
def build_record(normalized_order: dict, order_total: float, review_queue: str) -> dict:
    return {
        "order_id": normalized_order["order_id"],
        "customer": normalized_order["customer"],
        "country": normalized_order["country"],
        "order_total": order_total,
        "review_queue": review_queue,
    }


@node(output_name="fulfillment_summary")
def summarize_orders(fulfillment_records: list[dict], review_queues: list[str], order_totals: list[float]) -> dict:
    return {
        "orders_processed": len(fulfillment_records),
        "priority_orders": sum(1 for queue in review_queues if queue == "priority"),
        "manual_review_orders": sum(1 for queue in review_queues if queue == "manual_review"),
        "gross_revenue": round(sum(order_totals), 2),
    }


def build_quickstart_orders_graph() -> Graph:
    """Build a quickstart-style order processing graph."""
    order_graph = Graph(
        [normalize_order, compute_total, assign_queue, build_record],
        name="order_graph",
    )
    mapped_orders = (
        order_graph.as_node(name="process_orders")
        .rename_inputs(order="orders")
        .rename_outputs(
            fulfillment_record="fulfillment_records",
            review_queue="review_queues",
            order_total="order_totals",
        )
        .map_over("orders")
    )
    return Graph([mapped_orders, summarize_orders], name="quickstart_orders")


def main() -> None:
    graph = build_quickstart_orders_graph()
    runner = DaftRunner()
    orders = [
        {"order_id": "A-1", "customer": " ada ", "country": "us", "unit_price": 25, "quantity": 2},
        {"order_id": "B-2", "customer": "grace", "country": "uk", "unit_price": 80, "quantity": 2},
        {"order_id": "C-3", "customer": "linus", "country": "de", "unit_price": 10, "quantity": 1, "rush": True},
    ]

    result = runner.run(graph, {"orders": orders})
    print(result["fulfillment_summary"])
    print(result["fulfillment_records"])


if __name__ == "__main__":
    main()
