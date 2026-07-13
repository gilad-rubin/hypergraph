"""Control asynchronous Hypergraph work without awaiting submission."""

import asyncio
from contextlib import suppress

from hypergraph import AsyncRunner, Graph, node


async def demonstrate_live_control() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    @node(output_name="receipt")
    async def charge_order(order_id: str) -> str:
        entered.set()
        await release.wait()
        return f"charged {order_id}"

    runner = AsyncRunner()
    handle = runner.start_run(Graph([charge_order]), {"order_id": "order-200"})

    await asyncio.wait_for(entered.wait(), timeout=10)
    print(f"Before release: caller regained control; done={handle.done}")

    cancelled_waiter = asyncio.create_task(handle.result())
    await asyncio.sleep(0)  # let the waiter reach the shielded result call
    cancelled_waiter.cancel()
    with suppress(asyncio.CancelledError):
        await cancelled_waiter
    print(f"After cancelling one waiter: execution still live; done={handle.done}")

    release.set()
    result = await handle.result()
    print(f"After release: {result['receipt']}; done={handle.done}")


async def demonstrate_failure_inspection() -> None:
    @node(output_name="risk")
    async def score_order(order_id: str) -> str:
        await asyncio.sleep(0)
        if order_id == "order-bad":
            raise ValueError("risk service rejected order-bad")
        return "low"

    handle = AsyncRunner().start_map(
        Graph([score_order]),
        {"order_id": ["order-201", "order-bad", "order-202"]},
        map_over="order_id",
        max_concurrency=2,
    )
    batch = await handle.result(raise_on_failure=False)

    print(f"Inspected batch: requested={batch.requested_count}, settled={len(batch)}, failed={len(batch.failures)}")

    try:
        await handle.result()
    except ValueError as error:
        print(f"Default retrieval raises: {error}")


async def main() -> None:
    await demonstrate_live_control()
    await demonstrate_failure_inspection()


if __name__ == "__main__":
    asyncio.run(main())
