"""Control synchronous Hypergraph work without blocking the caller."""

from threading import Event

from hypergraph import Graph, SyncRunner, node


def demonstrate_live_control() -> None:
    entered = Event()
    release = Event()

    @node(output_name="receipt")
    def charge_order(order_id: str) -> str:
        entered.set()
        release.wait()
        return f"charged {order_id}"

    runner = SyncRunner()
    handle = runner.start_run(Graph([charge_order]), {"order_id": "order-100"})

    assert entered.wait(timeout=10), "background node never started"
    print(f"Before release: caller regained control; done={handle.done}")

    release.set()
    result = handle.result()
    print(f"After release: {result['receipt']}; done={handle.done}")


def demonstrate_failure_inspection() -> None:
    @node(output_name="risk")
    def score_order(order_id: str) -> str:
        if order_id == "order-bad":
            raise ValueError("risk service rejected order-bad")
        return "low"

    handle = SyncRunner().start_map(
        Graph([score_order]),
        {"order_id": ["order-101", "order-bad", "order-102"]},
        map_over="order_id",
    )
    batch = handle.result(raise_on_failure=False)

    print(f"Inspected batch: requested={batch.requested_count}, settled={len(batch)}, failed={len(batch.failures)}")

    try:
        handle.result()
    except ValueError as error:
        print(f"Default retrieval raises: {error}")


if __name__ == "__main__":
    demonstrate_live_control()
    demonstrate_failure_inspection()
