"""Inspect a real customer-review batch without adding persistence machinery.

Before: Maya had to correlate batch status, per-item logs, and failure objects.
After: one explicit inspect view keeps the batch, original item indexes, node
timeline, values, and exact failure together.

Notebook usage::

    batch = run_customer_review()
    batch.inspect()  # Keep this as the final expression.
"""

from __future__ import annotations

from hypergraph import AsyncRunner, Graph, MapResult, SyncRunner, node


@node(output_name="risk_score")
def score_customer(customer_id: str, lifetime_value: int) -> int:
    """Score one customer, escalating the known manual-review case."""
    if customer_id == "maya-23":
        raise ValueError("Customer maya-23 requires manual review")
    return 10 if lifetime_value >= 1_000 else 40


@node(output_name="review_action")
def choose_review_action(risk_score: int) -> str:
    """Turn the score into the action shown to the support team."""
    return "approve" if risk_score < 30 else "review"


CUSTOMER_REVIEW = Graph(
    [score_customer, choose_review_action],
    name="customer-review",
)


def run_customer_review() -> MapResult:
    """Run three original customer indexes, retaining one real failure."""
    return SyncRunner().map(
        CUSTOMER_REVIEW,
        {
            "customer_id": ["alex-10", "maya-23", "sam-04"],
            "lifetime_value": [2_400, 1_200, 3_100],
        },
        map_over=["customer_id", "lifetime_value"],
        inspect=True,
        error_handling="continue",
    )


async def run_customer_review_async() -> MapResult:
    """Run the same inspect contract with async execution."""
    return await AsyncRunner().map(
        CUSTOMER_REVIEW,
        {
            "customer_id": ["alex-10", "maya-23", "sam-04"],
            "lifetime_value": [2_400, 1_200, 3_100],
        },
        map_over=["customer_id", "lifetime_value"],
        inspect=True,
        error_handling="continue",
    )


if __name__ == "__main__":
    batch = run_customer_review()

    failed = next(result for result in batch.failures if result.failure is not None and result.failure.item_index == 1)
    failure = failed.failure
    assert failure is not None
    print(f"Before: {batch.status.value}; manual log and failure correlation required")
    print(f"After: original item {failure.item_index} — {failure.error}")
    print("Notebook next step: keep `batch.inspect()` as the final expression to open the rich view")
